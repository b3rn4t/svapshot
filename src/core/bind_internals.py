"""Promote DUT-internal references to checker ports and bind connections.

The historical approach rewrote internals to ``MODULE.signal``. That elaborates
under Simon (module-type hierarchical paths resolve) but breaks
``report_assertion_density``, which resolves names relative to the bound
checker instance. Density-safe checkers declare referenced internals as input
ports and wire them in the bind (``.*`` for top-level same-name nets, or an
explicit hierarchical map such as ``.rr_q(gen_arbiter.rr_q)``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from assertion_template import assertion_property_span
import rtl_clocking

_SV_LITERAL_BASES = frozenset('bBdDhHoO')

# Type keyword + packed dims only. A trailing ``\w+`` group here used to
# swallow the first declarator, so ``logic [W-1:0] foo = bar[0];`` matched
# names ``= bar[0]`` and fell back to a 1-bit port.
_DECL_RE = re.compile(
    r'\b(?:logic|reg|wire|bit|integer|int|byte|shortint|longint|time|'
    r'real|shortreal|string|genvar)\b'
    r'(?P<head>(?:\s+(?:signed|unsigned|automatic|static|const|var))*'
    r'(?:\s*\[[^\]]*\])*)'
    r'\s+(?P<names>[^;]+);',
    re.MULTILINE,
)

_TYPED_DECL_RE = re.compile(
    r'\b(?P<type>[A-Za-z_]\w*)\b'
    r'(?P<head>(?:[ \t]*\[[^\]]*\])*)'
    r'[ \t]+(?P<names>[^\n;]+);',
    re.MULTILINE,
)

_PARAM_DECL_RE = re.compile(
    r'\b(?:parameter|localparam)\b'
    r'(?P<head>[^=;]*?)'
    r'(?P<name>[A-Za-z_]\w*)\s*=',
    re.MULTILINE,
)

_BEGIN_LABEL_RE = re.compile(
    r'\bbegin\s*:\s*(?P<label>[A-Za-z_]\w*)',
    re.MULTILINE,
)
_END_RE = re.compile(r'\bend\b', re.MULTILINE)
_GENERATE_RE = re.compile(r'\bgenerate\b', re.MULTILINE)
_ENDGENERATE_RE = re.compile(r'\bendgenerate\b', re.MULTILINE)

_RFC_OBJ_RE = re.compile(
    r'RFC_OBJ_NOT_FOUND.*?(?:object|signal|net|variable)?\s*[\'"]?'
    r'(?P<path>[A-Za-z_][\w.]*)',
    re.IGNORECASE | re.DOTALL,
)
_RFC_PATH_LINE_RE = re.compile(
    r'(?:Cannot find|not found|Unable to find|unknown)\s+'
    r'(?:object|signal|net|variable)?\s*[\'"]?'
    r'(?P<path>[A-Za-z_][\w.]*(?:\.[A-Za-z_]\w*)+)',
    re.IGNORECASE,
)


@dataclass
class PortBinding:
    """One checker input promoted from a DUT-internal signal."""

    name: str
    decl: str
    hierarchical_path: Optional[str] = None


@dataclass
class RewriteResult:
    """Outcome of rewriting one assertion batch for density-safe binds."""

    assertions: List[str]
    needed_ports: Dict[str, PortBinding] = field(default_factory=dict)
    rewrite_count: int = 0
    rewritten_names: Set[str] = field(default_factory=set)
    unresolved_hierarchical: Set[str] = field(default_factory=set)


def looks_like_sv_literal_fragment(identifier: str) -> bool:
    """True when a token is part of a SystemVerilog literal, not a signal name."""
    if not identifier:
        return True
    if identifier.isdigit():
        return True
    if len(identifier) == 1 and identifier in _SV_LITERAL_BASES:
        return True
    return bool(
        re.fullmatch(r'[bB][01xzXZ?_]+', identifier)
        or re.fullmatch(r'[dD][0-9]+', identifier)
        or re.fullmatch(r'[hH][0-9a-fA-FxXzZ?_]+', identifier)
        or re.fullmatch(r'[oO][0-7xzXZ?_]+', identifier)
    )


def mask_sv_comments_and_strings(text: str) -> str:
    """Blank comments and string contents while preserving every source offset."""
    chars = list(text)
    index = 0
    state = 'code'
    while index < len(chars):
        char = chars[index]
        nxt = chars[index + 1] if index + 1 < len(chars) else ''
        if state == 'code':
            if char == '/' and nxt == '/':
                chars[index] = chars[index + 1] = ' '
                state = 'line_comment'
                index += 2
                continue
            if char == '/' and nxt == '*':
                chars[index] = chars[index + 1] = ' '
                state = 'block_comment'
                index += 2
                continue
            if char == '"':
                chars[index] = ' '
                state = 'string'
                index += 1
                continue
        elif state == 'line_comment':
            if char == '\n':
                state = 'code'
            else:
                chars[index] = ' '
            index += 1
            continue
        elif state == 'block_comment':
            if char == '*' and nxt == '/':
                chars[index] = chars[index + 1] = ' '
                state = 'code'
                index += 2
                continue
            if char != '\n':
                chars[index] = ' '
            index += 1
            continue
        else:  # string
            if char == '\\' and index + 1 < len(chars):
                chars[index] = chars[index + 1] = ' '
                index += 2
                continue
            if char == '"':
                chars[index] = ' '
                state = 'code'
            elif char != '\n':
                chars[index] = ' '
            index += 1
            continue
        index += 1
    return ''.join(chars)


def sub_in_sv_code(text: str, pattern: str, replacement) -> Tuple[str, int]:
    """``re.subn`` over code only, not comments or string literals."""
    masked = mask_sv_comments_and_strings(text)
    matches = list(re.finditer(pattern, masked))
    if not matches:
        return text, 0
    result = text
    for match in reversed(matches):
        value = replacement(match) if callable(replacement) else match.expand(replacement)
        result = result[:match.start()] + value + result[match.end():]
    return result, len(matches)


def unqualified_identifier_pattern(identifier: str) -> str:
    """Match a bare identifier that is not already hierarchically qualified."""
    return (
        rf'(?<!\$)'
        rf"(?<!\d')"
        rf"(?<!')"
        rf'(?<!\.)'
        rf'(?<!:)'
        rf'\b{re.escape(identifier)}\b'
    )


def module_qualified_pattern(module_name: str, identifier: str) -> str:
    """Match ``MODULE.id`` or ``MODULE.MODULE.id`` for stripping."""
    mod = re.escape(module_name)
    ident = re.escape(identifier)
    return rf'(?<!\.)(?:{mod}\.){{1,2}}{ident}\b'


def candidate_names(graph) -> Tuple[Set[str], Set[str]]:
    """Return ``(internals_needing_ports, parameters_to_unqualify)``."""
    ports = set(graph.ports_in) | set(graph.ports_out)
    internals = set(graph.internals) - ports
    parameters = set(graph.parameters) - ports
    return internals, parameters


def _unpacked_dims_for_name(names: str, name: str) -> str:
    """Unpacked ``[1:0]`` (etc.) attached to ``name`` in a declaration list."""
    match = re.search(
        rf'(?<!\.)\b{re.escape(name)}\b((?:\s*\[[^\]]*\])*)',
        names,
    )
    return (match.group(1) or '').strip() if match else ''


def _strip_declarator_initializers(names: str) -> str:
    """Drop ``= expr`` so ``foo = bar[0]`` still yields the declared name.

    Identifiers on the right-hand side must not be treated as the declarator
    (or as unpacked dimensions of a different signal).
    """
    out: List[str] = []
    index = 0
    depth = 0
    while index < len(names):
        char = names[index]
        if char == '[':
            depth += 1
            out.append(char)
        elif char == ']':
            depth = max(0, depth - 1)
            out.append(char)
        elif char == '=' and depth == 0:
            index += 1
            while index < len(names):
                nested = names[index]
                if nested == '[':
                    depth += 1
                elif nested == ']':
                    depth = max(0, depth - 1)
                elif nested == ',' and depth == 0:
                    break
                index += 1
            continue
        else:
            out.append(char)
        index += 1
    return ''.join(out)


def infer_signal_declaration(rtl_text: str, name: str) -> str:
    """Build a checker ``input`` declaration for ``name`` from RTL when possible."""
    masked = mask_sv_comments_and_strings(rtl_text)
    name_pat = re.compile(rf'(?<!\.)\b{re.escape(name)}\b')

    for match in _DECL_RE.finditer(masked):
        names = _strip_declarator_initializers(match.group('names'))
        if not name_pat.search(names):
            continue
        head = (match.group('head') or '').strip()
        # Prefer the keyword captured by scanning left of the match.
        start = match.start()
        keyword_match = re.search(
            r'\b(logic|reg|wire|bit|integer|int|byte|shortint|longint)\b\s*$',
            masked[max(0, start - 40):start + 1],
        )
        keyword = keyword_match.group(1) if keyword_match else 'logic'
        if keyword in ('reg', 'wire'):
            keyword = 'logic'
        width = ''
        dims = re.findall(r'\[[^\]]*\]', head)
        if dims:
            width = ' '.join(dims)
        unpacked = _unpacked_dims_for_name(names, name)
        pieces = ['input', keyword]
        if width:
            pieces.append(width)
        pieces.append(name)
        if unpacked:
            pieces.append(unpacked)
        return ' '.join(pieces)

    for match in _TYPED_DECL_RE.finditer(masked):
        type_name = match.group('type')
        if type_name.lower() in {
            'input', 'output', 'inout', 'module', 'endmodule', 'assign',
            'always', 'always_ff', 'always_comb', 'if', 'else', 'for',
            'begin', 'end', 'generate', 'parameter', 'localparam',
        }:
            continue
        names = match.group('names')
        if not name_pat.search(names):
            continue
        # Assignments such as ``valid_vector[i] = ptecache_entry[i].valid;``
        # look like typed declarations to this regex.
        if '=' in match.group(0):
            continue
        head = (match.group('head') or '').strip()
        if re.fullmatch(r'\[\s*[A-Za-z_]\w*\s*\]', head):
            continue
        unpacked = _unpacked_dims_for_name(names, name)
        keyword = type_name
        # DUT-local enums are a different type in $unit than in the DUT
        # module; .* bind then fails ENUMASSIGNTYPE. Flatten the port to
        # the underlying logic vector and keep the typedef for literals.
        if not type_name.endswith('_t'):
            flattened = _enum_logic_type(rtl_text, type_name)
            if flattened:
                keyword = flattened
                head = ''
        pieces = ['input', keyword]
        if head:
            pieces.append(head)
        pieces.append(name)
        if unpacked:
            pieces.append(unpacked)
        return ' '.join(pieces)

    return f'input logic {name}'


def _enum_logic_type(rtl_text: str, type_name: str) -> Optional[str]:
    """``logic [N:0]`` from ``typedef enum logic [N:0] {…} type_name``."""
    match = re.search(
        rf'typedef\s+enum\s+(logic(?:\s*\[[^\]]+\])?)\s*\{{[^}}]*\}}\s*'
        rf'{re.escape(type_name)}\b',
        rtl_text,
        re.DOTALL,
    )
    return match.group(1).strip() if match else None


_TYPEDEF_ENUM_RE = re.compile(
    r'typedef\s+enum\b.*?\}\s*(?P<name>[A-Za-z_]\w*)\s*;',
    re.DOTALL,
)


def local_enum_typedefs(rtl_text: str) -> List[Tuple[str, str]]:
    """DUT-local ``typedef enum`` blocks. Package imports do not see these."""
    masked = mask_sv_comments_and_strings(rtl_text)
    found: List[Tuple[str, str]] = []
    for match in _TYPEDEF_ENUM_RE.finditer(masked):
        name = match.group('name')
        found.append((name, rtl_text[match.start():match.end()].strip()))
    return found


def inject_local_enum_typedefs(prop_text: str, rtl_text: str) -> str:
    """Copy DUT-local enums into the checker so port types and literals compile."""
    missing = []
    for name, block in local_enum_typedefs(rtl_text):
        if re.search(
            rf'\btypedef\s+enum\b[^;]*\b{re.escape(name)}\s*;',
            prop_text,
            re.DOTALL,
        ):
            continue
        missing.append(block)
    if not missing:
        return prop_text
    banner = (
        '// DUT-local enum types (not visible via package import).\n'
        '// Copied so density-bind ports and enum literals compile in the checker.\n'
    )
    inserted = banner + '\n'.join(missing) + '\n\n'
    match = re.search(r'^module\s+\w+', prop_text, re.M)
    if match:
        return prop_text[:match.start()] + inserted + prop_text[match.start():]
    return inserted + prop_text


def find_hierarchical_path(rtl_text: str, name: str) -> Optional[str]:
    """Return a generate-scoped hierarchical path for ``name``, if any.

    Top-level DUT internals return ``None`` (``.*`` binds them by name). Nested
    names under ``begin : label`` return ``label.….name``.
    """
    masked = mask_sv_comments_and_strings(rtl_text)
    decl = re.search(
        rf'(?:^|;)\s*(?:(?:logic|reg|wire|bit|integer|int|[A-Za-z_]\w*)\b[^\n;]*?)'
        rf'(?<!\.)\b{re.escape(name)}\b[^\n;]*;',
        masked,
        re.MULTILINE,
    )
    if decl is None:
        return None

    target = decl.start()
    events: List[Tuple[int, str, str]] = []
    for match in _BEGIN_LABEL_RE.finditer(masked):
        events.append((match.start(), 'begin', match.group('label')))
    for match in _END_RE.finditer(masked):
        events.append((match.start(), 'end', ''))
    events.sort(key=lambda item: item[0])

    stack: List[str] = []
    for pos, kind, label in events:
        if pos > target:
            break
        if kind == 'begin':
            stack.append(label)
        elif stack:
            stack.pop()

    if not stack:
        return None
    return '.'.join(stack + [name])


def rewrite_assertions(
        assertions: Sequence[str],
        module_name: str,
        internals: Iterable[str],
        parameters: Iterable[str],
        rtl_text: str = '',
        hierarchical_overrides: Optional[Mapping[str, str]] = None,
) -> RewriteResult:
    """Strip ``MODULE.`` prefixes and collect ports that must be promoted.

    Parameters and enum labels are unqualified in place (the checker already
    mirrors DUT parameters). Internals become ``needed_ports`` with optional
    hierarchical bind paths.
    """
    internals_set = set(internals)
    parameters_set = set(parameters)
    candidates = internals_set | parameters_set
    overrides = dict(hierarchical_overrides or {})
    needed: Dict[str, PortBinding] = {}
    unresolved: Set[str] = set()
    processed: List[str] = []
    rewrite_count = 0
    rewritten_names: Set[str] = set()

    for assertion in assertions:
        span = assertion_property_span(assertion)
        if span is None:
            processed.append(assertion)
            continue

        start, end = span
        expression = assertion[start:end]

        for identifier in sorted(candidates, key=lambda item: (-len(item), item)):
            qualified = module_qualified_pattern(module_name, identifier)
            expression, count = sub_in_sv_code(
                expression, qualified, identifier)
            if count:
                rewrite_count += count
                rewritten_names.add(identifier)

            bare = unqualified_identifier_pattern(identifier)
            masked = mask_sv_comments_and_strings(expression)
            if re.search(bare, masked):
                if identifier in internals_set:
                    rewritten_names.add(identifier)
                    if identifier not in needed:
                        path = overrides.get(identifier)
                        if path is None and rtl_text:
                            path = find_hierarchical_path(rtl_text, identifier)
                        if path and '.' in path:
                            # Path includes the leaf name already.
                            pass
                        elif path is None and identifier in overrides:
                            path = overrides[identifier]
                        decl = infer_signal_declaration(rtl_text, identifier)
                        needed[identifier] = PortBinding(
                            name=identifier,
                            decl=decl,
                            hierarchical_path=path,
                        )
                        if path and path != identifier:
                            # Hierarchical: .* cannot connect; path recorded.
                            pass
                        elif path is None and _likely_nested_only(rtl_text, identifier):
                            unresolved.add(identifier)

        processed.append(assertion[:start] + expression + assertion[end:])

    for name, path in overrides.items():
        if name in internals_set and name not in needed:
            needed[name] = PortBinding(
                name=name,
                decl=infer_signal_declaration(rtl_text, name),
                hierarchical_path=path,
            )

    return RewriteResult(
        assertions=processed,
        needed_ports=needed,
        rewrite_count=rewrite_count,
        rewritten_names=rewritten_names,
        unresolved_hierarchical=unresolved,
    )


def _likely_nested_only(rtl_text: str, name: str) -> bool:
    """True when ``name`` appears only under a labeled begin (heuristic)."""
    if not rtl_text:
        return False
    path = find_hierarchical_path(rtl_text, name)
    return bool(path and '.' in path)


def existing_prop_parameters(prop_text: str) -> Set[str]:
    """Parameter names already declared on the checker ``#()`` list."""
    header = _prop_port_region(prop_text) or ''
    return {match.group('name') for match in _PARAM_DECL_RE.finditer(header)}


def dut_parameter_declarations(rtl_text: str) -> Dict[str, str]:
    """Map DUT ``parameter``/``localparam`` names to their declarations."""
    masked = mask_sv_comments_and_strings(rtl_text)
    decls: Dict[str, str] = {}
    for match in _PARAM_DECL_RE.finditer(masked):
        name = match.group('name')
        end = masked.find(';', match.start())
        if end < 0:
            continue
        decls[name] = ' '.join(rtl_text[match.start():end].split())
    return decls


def insert_parameters_in_prop(
        prop_text: str,
        decls: Sequence[Tuple[str, str]],
) -> str:
    """Insert parameter declarations at the start of the checker ``#()`` list."""
    if not decls:
        return prop_text
    added = ''.join(f'\t\t{decl},\n' for _name, decl in decls)
    match = re.search(r'(#\(\s*)', prop_text)
    if match:
        return prop_text[:match.end()] + added + prop_text[match.end():]
    match = re.search(r'(module\s+\w+\b)(\s*\()', prop_text)
    if match is None:
        return prop_text
    block = f'\n#(\n{added}\t\tparameter ASSERT_INPUTS = 0)'
    return prop_text[:match.end(1)] + block + '\n' + prop_text[match.start(2):]


def insert_parameters_in_bind(bind_text: str, names: Sequence[str]) -> str:
    """Bind each DUT parameter through to the checker instance."""
    present = set(re.findall(r'\.(\w+)\s*\(', bind_text))
    to_add = [name for name in names if name not in present]
    if not to_add:
        return bind_text
    added = ''.join(f'\t\t.{name} ({name}),\n' for name in to_add)
    match = re.search(r'(#\(\s*)', bind_text)
    if match:
        return bind_text[:match.end()] + added + bind_text[match.end():]
    return bind_text


def inject_dut_parameters(
        prop_text: str,
        bind_text: str,
        rtl_text: str,
        parameter_names: Optional[Iterable[str]] = None,
) -> Tuple[str, str]:
    """Declare DUT parameters on the checker so port widths elaborate.

    Verilog-95 body parameters never enter the scaffold ``#()`` copy, so a
    checker port ``[width-1:0]`` fails VCF with ``Identifier not declared``.
    """
    decls = dut_parameter_declarations(rtl_text)
    if parameter_names is None:
        wanted = set(decls)
    else:
        wanted = {name for name in parameter_names if name in decls}
    already = existing_prop_parameters(prop_text)
    to_add = sorted(wanted - already)
    if not to_add:
        return prop_text, bind_text
    new_prop = insert_parameters_in_prop(
        prop_text, [(name, decls[name]) for name in to_add])
    new_bind = insert_parameters_in_bind(bind_text, to_add)
    return new_prop, new_bind


def existing_prop_ports(prop_text: str) -> Set[str]:
    """Port names already declared on the checker module."""
    header = _prop_port_region(prop_text)
    if header is None:
        return set()
    names = set()
    skip = {
        'input', 'output', 'inout', 'logic', 'wire', 'reg', 'bit', 'signed',
        'unsigned', 'integer', 'int', 'var',
    }
    for raw in header.splitlines():
        line = raw.split('//', 1)[0]
        if not re.search(r'\b(?:input|output|inout)\b', line):
            continue
        idents = [
            ident for ident in re.findall(r'[A-Za-z_]\w*', line)
            if ident not in skip
        ]
        if idents:
            names.add(idents[-1])
    return names


def _prop_port_region(prop_text: str) -> Optional[str]:
    match = re.search(
        r'module\s+\w+\b.*?\)\s*;',
        prop_text,
        re.DOTALL,
    )
    return match.group(0) if match else None


def promote_ports_in_prop(
        prop_text: str,
        needed_ports: Mapping[str, PortBinding],
) -> str:
    """Insert missing promoted inputs into the checker port list."""
    if not needed_ports:
        return prop_text

    already = existing_prop_ports(prop_text)
    to_add = [
        binding for name, binding in sorted(needed_ports.items())
        if name not in already
    ]
    if not to_add:
        return prop_text

    # Match the closing ``);`` of the port list (after parameters if any).
    match = re.search(
        r'(module\s+\w+\b.*?)(\n\t\);|\n\);|\);)',
        prop_text,
        re.DOTALL,
    )
    if match is None:
        return prop_text

    prefix = match.group(1).rstrip()
    # Ensure the previous port line ends with a comma.
    if not prefix.rstrip().endswith(','):
        # Insert comma after the last non-comment port token.
        lines = prefix.splitlines()
        for index in range(len(lines) - 1, -1, -1):
            stripped = lines[index].split('//', 1)[0].rstrip()
            if not stripped:
                continue
            if stripped.endswith(','):
                break
            if stripped.endswith('(') or stripped.endswith('#('):
                break
            comment = ''
            if '//' in lines[index]:
                code, comment = lines[index].split('//', 1)
                lines[index] = code.rstrip() + ',' + ' //' + comment
            else:
                lines[index] = lines[index].rstrip() + ','
            break
        prefix = '\n'.join(lines)

    additions = []
    for binding in to_add:
        additions.append(f'\t\t{binding.decl}, // internal (density bind)')
    # Last added port should not leave a trailing comma issue — the closing
    # ``);`` follows; keep a trailing comma only if SV style in file uses it.
    # the scaffolder files omit a trailing comma on the final port, so strip the last.
    if additions:
        additions[-1] = additions[-1].replace(', //', ' //', 1)

    inserted = prefix + '\n' + '\n'.join(additions) + '\n\t);'
    return prop_text[:match.start()] + inserted + prop_text[match.end():]


def update_bind_connections(
        bind_text: str,
        needed_ports: Mapping[str, PortBinding],
) -> str:
    """Add explicit hierarchical port maps; leave ``.*`` for same-name nets."""
    explicit = {
        name: binding.hierarchical_path
        for name, binding in needed_ports.items()
        if binding.hierarchical_path and binding.hierarchical_path != name
    }
    if not explicit:
        return bind_text

    # Already-present named connections.
    present = set(re.findall(r'\.(\w+)\s*\(', bind_text))
    to_add = {
        name: path for name, path in sorted(explicit.items())
        if name not in present
    }
    if not to_add:
        return bind_text

    lines = []
    for name, path in to_add.items():
        lines.append(f'\t\t.{name}({path}),')

    # Prefer inserting immediately before ``.*`` when present.
    star = re.search(r'(\)\s+u_\w+_sva\s*\(\s*)\.\*\s*\);', bind_text)
    if star:
        insert_at = star.start(1) + len(star.group(1))
        block = '\n' + '\n'.join(lines) + '\n\t\t'
        return bind_text[:insert_at] + block + '.*);' + bind_text[star.end():]

    # Fallback: before the final ``);`` of the bind instance.
    closing = re.search(r'\)\s*;\s*$', bind_text, re.MULTILINE)
    if closing is None:
        return bind_text
    # Walk back to the instance port list open paren of u_*_sva(
    inst = re.search(r'u_\w+_sva\s*\(', bind_text)
    if inst is None:
        return bind_text
    # Replace empty or parameter-only bind body.
    body_start = inst.end()
    body_end = closing.start()
    body = bind_text[body_start:body_end].rstrip()
    if body and not body.endswith(','):
        body += ','
    addition = '\n' + '\n'.join(lines)
    return bind_text[:body_start] + body + addition + '\n\t);' + bind_text[closing.end():]


def parse_rfc_obj_not_found(log_text: str) -> List[str]:
    """Extract hierarchical paths mentioned in RFC_OBJ_NOT_FOUND diagnostics."""
    found: List[str] = []
    seen: Set[str] = set()
    for pattern in (_RFC_PATH_LINE_RE, _RFC_OBJ_RE):
        for match in pattern.finditer(log_text):
            path = match.group('path')
            if '.' not in path:
                continue
            if path in seen:
                continue
            seen.add(path)
            found.append(path)
    return found


def hierarchical_overrides_from_rfc(paths: Sequence[str]) -> Dict[str, str]:
    """Map leaf port name → hierarchical path from RFC diagnostics."""
    overrides: Dict[str, str] = {}
    for path in paths:
        leaf = path.rsplit('.', 1)[-1]
        # Prefer the longest path when duplicates appear.
        if leaf not in overrides or len(path) > len(overrides[leaf]):
            overrides[leaf] = path
    return overrides


def generate_density_probe_tcl(
        module_name: str,
        *,
        files_vcf_basename: str = 'files_vcf.vc',
        module_type: str = 'sequential',
        clk_sig: Optional[str] = None,
        rst_sig: Optional[str] = None,
        rst_active_low: bool = True,
) -> str:
    """Return a short VC Formal script: elaborate + assertion density only."""
    lines = [
        f'# VC Formal density probe for {module_name}',
        '# Auto-generated by bind_internals.generate_density_probe_tcl',
        '# Stops after elaborate + report_assertion_density (no check_fv).',
        '',
        f'set top {module_name}',
        'set_fml_appmode FPV',
        'suppress_message SM_UST',
        f'set files_vcf [file join [file dirname [info script]] {files_vcf_basename}]',
        'analyze -format sverilog -vcs "-f $files_vcf"',
        'elaborate -sva $top',
        '',
    ]
    if module_type == 'combinational':
        formal_clk = rtl_clocking.combinational_clock_hier(module_name)
        lines.append(f'create_clock {{{formal_clk}}} -period 100')
        lines.append('')
    elif clk_sig:
        lines.append(f'create_clock {clk_sig} -period 100')
        if rst_sig:
            sense = 'low' if rst_active_low else 'high'
            lines.append(f'create_reset {rst_sig} -sense {sense}')
        lines.append('')
    lines.extend([
        'set report_dir [file join [file dirname [info script]] .. '
        f'vcf_projs {module_name} reports]',
        'file mkdir $report_dir',
        'catch {',
        '    redirect -file $report_dir/density_probe.rpt {',
        '        echo "DENSITY_SCOPE reg"',
        '        catch {report_assertion_density -list -status all -type reg}',
        '        echo "DENSITY_SCOPE pi"',
        '        catch {report_assertion_density -list -status all -type pi}',
        '    }',
        '}',
        '',
    ])
    return '\n'.join(lines)


def apply_port_promotion(
        assertions: Sequence[str],
        *,
        module_name: str,
        graph,
        rtl_text: str,
        prop_text: str,
        bind_text: str,
        hierarchical_overrides: Optional[Mapping[str, str]] = None,
) -> Tuple[RewriteResult, str, str]:
    """Rewrite assertions and return updated prop/bind file texts."""
    internals, parameters = candidate_names(graph)
    result = rewrite_assertions(
        assertions,
        module_name,
        internals,
        parameters,
        rtl_text=rtl_text,
        hierarchical_overrides=hierarchical_overrides,
    )
    new_prop = inject_local_enum_typedefs(prop_text, rtl_text)
    new_prop, new_bind = inject_dut_parameters(new_prop, bind_text, rtl_text)
    new_prop = promote_ports_in_prop(new_prop, result.needed_ports)
    new_bind = update_bind_connections(new_bind, result.needed_ports)
    return result, new_prop, new_bind
