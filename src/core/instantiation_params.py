"""Direct-submodule instantiation parameters for prompts and FPV elaborate.

When a leaf is proved as its own formal top, VCS uses the leaf header
defaults. Those can be an illegal combination the parent never elaborates
(``Width=32`` with every FP format enabled). This module:

1. Reads the parent's direct ``#()`` instantiations.
2. Keeps shared literals vs differing / passthrough expressions for prompts.
3. Folds passthrough expressions to **compile-time literals** when the
   parent (and its packages) define them as constants — then those
   literals are applied at leaf ``elaborate`` via VCS ``-pvalue``.

The resolver is not a SystemVerilog elaborator. It only follows
identifier chains, ``pkg::name``, named assignment-pattern fields, and
simple casts. Function calls, generate indices, types, and anything that
does not become a numeric / based literal are left alone (leaf default).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple


_SV_KEYWORDS = frozenset({
    'logic', 'wire', 'reg', 'bit', 'byte', 'shortint', 'int', 'longint',
    'input', 'output', 'inout', 'parameter', 'localparam', 'const',
    'assign', 'always', 'always_ff', 'always_comb', 'initial',
    'if', 'else', 'case', 'casex', 'casez', 'default', 'for', 'while',
    'repeat', 'foreach', 'do', 'generate', 'genvar', 'begin', 'end',
    'function', 'task', 'return', 'void', 'class', 'interface', 'modport',
    'typedef', 'struct', 'union', 'enum', 'string', 'event',
    'mailbox', 'semaphore', 'process', 'time', 'realtime',
    'and', 'or', 'not', 'nand', 'nor', 'xor', 'xnor', 'buf', 'bufif0', 'bufif1',
    'assert', 'assume', 'cover', 'property', 'sequence', 'clocking', 'disable',
    'module', 'endmodule',
    'endcase', 'endfunction', 'endtask', 'endgenerate', 'endinterface',
    'endpackage', 'endclass', 'endproperty', 'endsequence', 'endclocking',
    'endprogram', 'endspecify', 'endchecker', 'endconfig',
    'unique', 'unique0', 'priority', 'forever', 'break', 'continue',
    'wait', 'fork', 'join', 'join_any', 'join_none', 'package',
    'import', 'export', 'virtual', 'extern', 'static', 'automatic',
})

_MODULE_INST_RE = re.compile(
    r'''
    ^[ \t]*
    (?P<module>\w+)
    (?:
        \s*\#[ \t]*\(
            (?P<params>
                (?:[^()]|\([^()]*\))*
            )
        \)\s*
      |
        [ \t]+
    )
    (?P<instance>\w+)
    \s*\(
    ''',
    re.MULTILINE | re.VERBOSE | re.DOTALL,
)

_NAMED_OVERRIDE_RE = re.compile(
    r'''
    \.
    (?P<name>\w+)
    \s*\(
        (?P<value>
            (?:[^()]|\([^()]*\))*
        )
    \)
    ''',
    re.VERBOSE | re.DOTALL,
)

_HEADER_PARAM_RE = re.compile(
    r'''
    \bparameter\b
    [^,;=]*?
    \b(?P<name>[A-Za-z_]\w*)\s*=\s*
    (?P<value>[^,;]+)
    ''',
    re.VERBOSE,
)


#: Literals are fixed polarities the prompt should target. Bare identifiers
#: (``.Width(WIDTH)``, ``.NumIn(NUM_OPGROUPS)``) forward a parent expression
#: and must stay parametric.
_LITERAL_VALUE_RE = re.compile(
    r'''^(?:
        \d+\s*'\s*[sS]?[bBhHdDoO]\s*[0-9a-fA-FxXzZ_]+
      | \d+
      | true
      | false
    )$''',
    re.VERBOSE | re.IGNORECASE,
)


@dataclass(frozen=True)
class InstanceOverrides:
    """One direct instantiation of a submodule under a parent."""

    module: str
    instance: str
    overrides: Mapping[str, str]


@dataclass
class ModuleInstantiationContext:
    """Aggregated parent instantiations of one submodule type."""

    module: str
    instances: List[InstanceOverrides] = field(default_factory=list)
    #: Parameter -> value when every instance overrides it to the same
    #: expression (after whitespace normalisation).
    shared_overrides: Dict[str, str] = field(default_factory=dict)
    #: Parameter -> parent expression when every instance uses a non-literal
    #: override (``.Width(WIDTH)``, ``.NumIn(NUM_OPGROUPS)``). Keep parametric.
    passthrough_overrides: Dict[str, str] = field(default_factory=dict)
    #: Parameter -> [(instance, value), ...] when instances disagree.
    differing_overrides: Dict[str, List[Tuple[str, str]]] = field(
        default_factory=dict)
    #: Parameter -> leaf-header default for parameters no parent instance
    #: overrides. These still fix functionality under the parent.
    untouched_defaults: Dict[str, str] = field(default_factory=dict)
    #: Parameter -> literal that every instance shares after constant
    #: folding. Applied at leaf FPV elaborate; also shown in the prompt.
    resolved_elaboration_overrides: Dict[str, str] = field(default_factory=dict)

    @property
    def has_guidance(self) -> bool:
        return bool(
            self.instances
            or self.shared_overrides
            or self.passthrough_overrides
            or self.differing_overrides
            or self.untouched_defaults
            or self.resolved_elaboration_overrides
        )


def strip_sv_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments without touching string contents."""
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r'//.*?$', '', text, flags=re.MULTILINE)
    return text


def normalise_value(value: str) -> str:
    """Collapse whitespace so equivalent overrides compare equal."""
    return re.sub(r'\s+', ' ', value.strip())


def parse_named_overrides(param_block: Optional[str]) -> Dict[str, str]:
    """Parse ``.Name(value)`` overrides from a ``#(...)`` body."""
    if not param_block:
        return {}
    overrides = {}
    for match in _NAMED_OVERRIDE_RE.finditer(param_block):
        overrides[match.group('name')] = normalise_value(match.group('value'))
    return overrides


def extract_direct_instances(parent_rtl_text: str) -> List[InstanceOverrides]:
    """Return every direct submodule instantiation in a parent module body.

    Only the parent's own instantiations are considered: deeper hierarchy is
    out of scope because the flow only generates properties for direct
    submodules.
    """
    content = strip_sv_comments(parent_rtl_text)
    instances: List[InstanceOverrides] = []
    seen = set()

    for match in _MODULE_INST_RE.finditer(content):
        module = match.group('module')
        instance = match.group('instance')
        if module in _SV_KEYWORDS or not module.isidentifier():
            continue
        if module.startswith('$') or module.isdigit():
            continue
        key = (module, instance)
        if key in seen:
            continue
        seen.add(key)
        overrides = parse_named_overrides(match.group('params'))
        instances.append(InstanceOverrides(
            module=module,
            instance=instance,
            overrides=overrides,
        ))
    return instances


def extract_module_parameter_defaults(leaf_rtl_text: str) -> Dict[str, str]:
    """Read ``parameter Name = value`` defaults from a module header."""
    content = strip_sv_comments(leaf_rtl_text)
    header_match = re.search(
        r'\bmodule\b\s+\w+\s*(?:#\s*\((?P<body>.*?)\))?\s*\(',
        content,
        re.DOTALL,
    )
    if not header_match or header_match.group('body') is None:
        return {}
    defaults = {}
    for match in _HEADER_PARAM_RE.finditer(header_match.group('body')):
        defaults[match.group('name')] = normalise_value(match.group('value'))
    return defaults


def is_constant_override(value: str) -> bool:
    """True when the override is a numeric / bit literal, not a parent name."""
    return bool(_LITERAL_VALUE_RE.match(normalise_value(value)))


def is_passthrough_override(name: str, value: str) -> bool:
    """True when the override is a parent expression rather than a literal."""
    return not is_constant_override(value)


_FILL_LITERAL_RE = re.compile(r"^'[01xXzZ]$")
_IDENT_RE = re.compile(r'^[A-Za-z_]\w*$')
_MAX_RESOLVE_DEPTH = 12


def is_elaboratable_literal(value: str) -> bool:
    """True when VCS ``-pvalue`` can take this as a top-level parameter value."""
    text = normalise_value(value)
    return is_constant_override(text) or bool(_FILL_LITERAL_RE.match(text))


_BASED_LITERAL_RE = re.compile(
    r"""^(?:(\d+)\s*)?'\s*([sS]?)([bBhHdDoO])\s*([0-9a-fA-FxXzZ_]+)$"""
)


def sv_literal_to_pvalue(value: str) -> Optional[str]:
    """Quote-free decimal (or 0/1) for VCS ``-pvalue``.

    VC Formal launches VCS with ``sh -c``. A SystemVerilog based literal
    such as ``1'b1`` or ``5'b11111`` contains ``'`` and breaks that shell
    line (``unexpected EOF while looking for matching '\\''``). Decimal
    integers are legal ``-pvalue`` values and stay shell-safe.
    """
    text = normalise_value(value)
    if re.fullmatch(r'\d+', text):
        return text
    lowered = text.lower()
    if lowered == 'true':
        return '1'
    if lowered == 'false':
        return '0'
    match = _BASED_LITERAL_RE.match(text)
    if not match:
        return None
    digits = match.group(4).replace('_', '')
    if re.search(r'[xXzZ]', digits):
        return None
    bases = {'b': 2, 'o': 8, 'd': 10, 'h': 16}
    try:
        number = int(digits, bases[match.group(3).lower()])
    except ValueError:
        return None
    return str(number)


def extract_imported_packages(sv_text: str) -> List[str]:
    """Package names from ``import pkg::`` / ``import pkg::*``."""
    return re.findall(r'\bimport\s+(\w+)::', strip_sv_comments(sv_text))


def extract_package_name(sv_text: str) -> Optional[str]:
    """First ``package`` declaration in a file, if any."""
    match = re.search(r'\bpackage\s+(\w+)', strip_sv_comments(sv_text))
    return match.group(1) if match else None


def _mask_function_and_task_bodies(text: str) -> str:
    """Drop function/task bodies so their localparams do not enter the env."""
    text = re.sub(r'\bfunction\b.*?\bendfunction\b', '', text, flags=re.DOTALL)
    return re.sub(r'\btask\b.*?\bendtask\b', '', text, flags=re.DOTALL)


def _read_sv_expr(text: str) -> Tuple[str, str]:
    """Read one expression until a depth-0 comma, semicolon, or unmatched ``)``."""
    depth_p = depth_b = depth_c = 0
    for index, char in enumerate(text):
        if char == '(':
            depth_p += 1
        elif char == ')':
            if depth_p == 0 and depth_b == 0 and depth_c == 0:
                return text[:index].strip(), text[index:]
            depth_p -= 1
        elif char == '[':
            depth_b += 1
        elif char == ']':
            depth_b -= 1
        elif char == '{':
            depth_c += 1
        elif char == '}':
            depth_c -= 1
        elif char in ',;' and depth_p == depth_b == depth_c == 0:
            return text[:index].strip(), text[index:]
    return text.strip(), ''


def _parse_name_equals_value(text: str) -> Tuple[Optional[str], Optional[str]]:
    """After ``parameter`` / ``localparam``, return ``(Name, expr)``."""
    depth_p = depth_b = depth_c = 0
    last_ident = ''
    token = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == '(':
            depth_p += 1
            token = []
        elif char == ')':
            if depth_p == 0 and depth_b == 0 and depth_c == 0:
                return None, None
            depth_p -= 1
            token = []
        elif char == '[':
            depth_b += 1
            token = []
        elif char == ']':
            depth_b -= 1
            token = []
        elif char == '{':
            depth_c += 1
            token = []
        elif char == '}':
            depth_c -= 1
            token = []
        elif depth_p == depth_b == depth_c == 0:
            if char == '=' and not (index + 1 < len(text) and text[index + 1] == '='):
                name = last_ident
                value, _ = _read_sv_expr(text[index + 1:])
                if name and _IDENT_RE.match(name):
                    return name, value
                return None, None
            if char.isalnum() or char == '_':
                token.append(char)
                last_ident = ''.join(token)
            else:
                token = []
        index += 1
    return None, None


def extract_constant_bindings(sv_text: str) -> Dict[str, str]:
    """``parameter`` / ``localparam`` name → raw RHS from a module or package."""
    content = _mask_function_and_task_bodies(strip_sv_comments(sv_text))
    bindings: Dict[str, str] = {}
    for match in re.finditer(r'\b(?:parameter|localparam)\b', content):
        name, value = _parse_name_equals_value(content[match.end():])
        if name and value:
            bindings[name] = normalise_value(value)
    return bindings


def _split_top_level(text: str, separator: str) -> List[str]:
    parts = []
    depth_p = depth_b = depth_c = 0
    start = 0
    for index, char in enumerate(text):
        if char == '(':
            depth_p += 1
        elif char == ')':
            depth_p -= 1
        elif char == '[':
            depth_b += 1
        elif char == ']':
            depth_b -= 1
        elif char == '{':
            depth_c += 1
        elif char == '}':
            depth_c -= 1
        elif (char == separator and depth_p == depth_b == depth_c == 0):
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def extract_assignment_pattern_fields(value: str) -> Optional[Dict[str, str]]:
    """Named fields of ``'{ Name: expr, ... }`` / ``{ Name: expr, ... }``."""
    text = normalise_value(value)
    if text.startswith("'{") and text.endswith('}'):
        body = text[2:-1]
    elif text.startswith('{') and text.endswith('}') and ':' in text:
        body = text[1:-1]
    else:
        return None
    fields: Dict[str, str] = {}
    for part in _split_top_level(body, ','):
        part = part.strip()
        match = re.match(r'^([A-Za-z_]\w*)\s*:\s*(.*)$', part, re.DOTALL)
        if not match:
            continue
        fields[match.group(1)] = normalise_value(match.group(2))
    return fields or None


def _split_cast(expr: str) -> Optional[Tuple[str, str]]:
    """Split ``type'(inner)suffix`` into ``(inner, suffix)``."""
    marker = expr.find("'(")
    if marker < 0:
        return None
    inner, rest = _read_sv_expr(expr[marker + 2:])
    if not rest.startswith(')'):
        return None
    return inner, rest[1:].strip()


def _lookup_name(
    name: str,
    parent_env: Mapping[str, str],
    package_envs: Mapping[str, Mapping[str, str]],
    imported_packages: Sequence[str],
) -> Optional[str]:
    if name in parent_env:
        return parent_env[name]
    for package in imported_packages:
        if name in package_envs.get(package, {}):
            return package_envs[package][name]
    return None


def resolve_constant_expr(
    expr: str,
    parent_env: Mapping[str, str],
    package_envs: Optional[Mapping[str, Mapping[str, str]]] = None,
    imported_packages: Optional[Sequence[str]] = None,
    stack: Optional[Set[str]] = None,
    depth: int = 0,
) -> Optional[str]:
    """Fold ``expr`` to a simpler constant, or ``None`` if it is not static.

    Follows parent / package identifiers, ``pkg::name``, named assignment-
    pattern fields, and ``type'(expr)``. Stops on function calls, generate
    indices, types, and unknown names.
    """
    text = normalise_value(expr)
    packages = package_envs or {}
    imported = list(imported_packages or [])
    seen = set(stack or ())
    if not text or depth > _MAX_RESOLVE_DEPTH:
        return None
    if is_elaboratable_literal(text):
        return text
    # Keep named assignment patterns so ``Features.Width`` can select a field
    # after ``Features`` folds to ``'{ Width: 64, ... }``.
    if extract_assignment_pattern_fields(text) is not None:
        return text

    scoped = re.match(r'^([A-Za-z_]\w*)::([A-Za-z_]\w*)(.*)$', text)
    if scoped:
        package, name, suffix = scoped.group(1), scoped.group(2), scoped.group(3)
        inner = packages.get(package, {}).get(name)
        if inner is None:
            return None
        key = f'{package}::{name}'
        if key in seen:
            return None
        resolved = resolve_constant_expr(
            inner, parent_env, packages, imported, seen | {key}, depth + 1)
        if resolved is None:
            return None
        return _apply_constant_suffix(
            resolved, suffix, parent_env, packages, imported, seen, depth)

    cast = _split_cast(text)
    if cast is not None:
        inner, suffix = cast
        resolved = resolve_constant_expr(
            inner, parent_env, packages, imported, seen, depth + 1)
        if resolved is None:
            return None
        return _apply_constant_suffix(
            resolved, suffix, parent_env, packages, imported, seen, depth)

    if re.match(r'^[A-Za-z_]\w*\s*\(', text):
        return None

    named = re.match(r'^([A-Za-z_]\w*)(.*)$', text)
    if not named:
        return None
    name, suffix = named.group(1), named.group(2)
    inner = _lookup_name(name, parent_env, packages, imported)
    if inner is None:
        return None
    if name in seen:
        return None
    resolved = resolve_constant_expr(
        inner, parent_env, packages, imported, seen | {name}, depth + 1)
    if resolved is None:
        return None
    return _apply_constant_suffix(
        resolved, suffix, parent_env, packages, imported, seen, depth)


def _apply_constant_suffix(
    value: str,
    suffix: str,
    parent_env: Mapping[str, str],
    package_envs: Mapping[str, Mapping[str, str]],
    imported_packages: Sequence[str],
    stack: Set[str],
    depth: int,
) -> Optional[str]:
    text = suffix.strip()
    if not text:
        return value
    field = re.match(r'^\.([A-Za-z_]\w*)(.*)$', text)
    if field:
        fields = extract_assignment_pattern_fields(value)
        if not fields or field.group(1) not in fields:
            return None
        return resolve_constant_expr(
            fields[field.group(1)] + field.group(2),
            parent_env, package_envs, imported_packages, stack, depth + 1)
    return None


def build_resolution_env(
    parent_rtl_text: str,
    package_texts: Optional[Mapping[str, str]] = None,
) -> Tuple[Dict[str, str], Dict[str, Dict[str, str]], List[str]]:
    """Parent bindings, per-package bindings, and imported package names."""
    parent_env = extract_constant_bindings(parent_rtl_text)
    package_envs: Dict[str, Dict[str, str]] = {}
    for name, text in (package_texts or {}).items():
        package_envs[name] = extract_constant_bindings(text)
        declared = extract_package_name(text)
        if declared and declared not in package_envs:
            package_envs[declared] = package_envs[name]
    imported = extract_imported_packages(parent_rtl_text)
    return parent_env, package_envs, imported


def resolve_elaboration_overrides(
    context: ModuleInstantiationContext,
    parent_rtl_text: str,
    package_texts: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Shared parent overrides that fold to literals for leaf elaborate.

    Differing instance values are skipped. Unresolvable expressions
    (``.OpGroup(opgroup_e'(opgrp))``, function calls) are skipped. The
    result never invents a value that is not already a parent constant.
    """
    parent_env, package_envs, imported = build_resolution_env(
        parent_rtl_text, package_texts)
    candidates: Dict[str, str] = {}
    candidates.update(context.shared_overrides)
    candidates.update(context.passthrough_overrides)
    resolved: Dict[str, str] = {}
    for name, value in candidates.items():
        folded = resolve_constant_expr(
            value, parent_env, package_envs, imported)
        if folded is not None and is_elaboratable_literal(folded):
            resolved[name] = folded
    return resolved


def format_vcs_pvalue_string(
    module_name: str,
    overrides: Mapping[str, str],
) -> str:
    """Space-separated ``-pvalue+mod.name=value`` flags for a Tcl ``-vcs`` list.

    Based literals are rewritten to decimal so the flags survive
    ``sh -c`` (see :func:`sv_literal_to_pvalue`).
    """
    parts = []
    for name, value in sorted(overrides.items()):
        if not _IDENT_RE.match(name) or not _IDENT_RE.match(module_name):
            continue
        pvalue = sv_literal_to_pvalue(value)
        if pvalue is None:
            continue
        if re.search(r"""[\s"'{}\\`$;&|<>()]""", pvalue):
            continue
        parts.append(f'-pvalue+{module_name}.{name}={pvalue}')
    return ' '.join(parts)


def elaboration_overrides_path(module_name: str, cwd: Optional[str] = None) -> str:
    """JSON beside the checker so TCL regenerate can reload the overrides."""
    root = cwd or os.getcwd()
    return os.path.join(root, f'ft_{module_name}', 'sva',
                        'elaboration_parameters.json')


def write_elaboration_overrides(
    module_name: str,
    overrides: Mapping[str, str],
    cwd: Optional[str] = None,
) -> str:
    """Write resolved literals for ``module_name``; return the path."""
    path = elaboration_overrides_path(module_name, cwd=cwd)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(dict(overrides), handle, indent=2, sort_keys=True)
        handle.write('\n')
    return path


def read_elaboration_overrides(
    module_name: str,
    cwd: Optional[str] = None,
) -> Dict[str, str]:
    """Return stored elaborate literals, or ``{}`` if the file is absent."""
    path = elaboration_overrides_path(module_name, cwd=cwd)
    if not os.path.isfile(path):
        return {}
    with open(path, encoding='utf-8') as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        return {}
    return {
        str(name): str(value)
        for name, value in payload.items()
        if _IDENT_RE.match(str(name)) and is_elaboratable_literal(str(value))
    }


def aggregate_module_context(
    module: str,
    instances: Sequence[InstanceOverrides],
    leaf_defaults: Optional[Mapping[str, str]] = None,
) -> ModuleInstantiationContext:
    """Collapse the parent's instances of ``module`` into prompt guidance."""
    of_type = [inst for inst in instances if inst.module == module]
    context = ModuleInstantiationContext(module=module, instances=list(of_type))
    if not of_type:
        return context

    all_names = set()
    for inst in of_type:
        all_names.update(inst.overrides)

    for name in sorted(all_names):
        values_by_instance = []
        for inst in of_type:
            if name in inst.overrides:
                values_by_instance.append((inst.instance, inst.overrides[name]))
        if not values_by_instance:
            continue
        unique_values = {value for _, value in values_by_instance}
        if (len(values_by_instance) == len(of_type)
                and len(unique_values) == 1):
            value = values_by_instance[0][1]
            if is_passthrough_override(name, value):
                context.passthrough_overrides[name] = value
            else:
                context.shared_overrides[name] = value
        else:
            context.differing_overrides[name] = values_by_instance

    overridden = (
        set(context.shared_overrides)
        | set(context.differing_overrides)
        | set(context.passthrough_overrides)
    )
    for name, default in (leaf_defaults or {}).items():
        if name == 'ASSERT_INPUTS':
            continue
        if name not in overridden:
            context.untouched_defaults[name] = default

    return context


def collect_parent_contexts(
    parent_rtl_text: str,
    leaf_defaults_by_module: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> Dict[str, ModuleInstantiationContext]:
    """Build a context per directly-instantiated submodule type."""
    instances = extract_direct_instances(parent_rtl_text)
    defaults = leaf_defaults_by_module or {}
    modules = sorted({inst.module for inst in instances})
    return {
        module: aggregate_module_context(
            module, instances, defaults.get(module))
        for module in modules
    }


def format_instantiation_prompt(context: ModuleInstantiationContext) -> str:
    """Human-readable block injected into initial and extension prompts."""
    if not context.has_guidance:
        return ''

    lines = [
        f'PARENT INSTANTIATION CONTEXT for {context.module}',
        'This checker is bound into a parent design. The bind passes each',
        "instance's parameter values through, so a property whose antecedent",
        'requires a value the parent never uses is vacuously true and adds no',
        'coverage. Target the configurations below.',
        '',
        'Direct instances under the parent:',
    ]
    for inst in context.instances:
        if inst.overrides:
            override_text = ', '.join(
                f'{name}={value}'
                for name, value in sorted(inst.overrides.items())
            )
        else:
            override_text = '(no #() overrides; module defaults apply)'
        lines.append(f'  - {inst.instance}: {override_text}')

    if context.shared_overrides:
        lines.append('')
        lines.append(
            'Parameters with the same override in every instance — target '
            'these polarities and values:')
        for name, value in sorted(context.shared_overrides.items()):
            lines.append(f'  - {name} = {value}')
        lines.append(
            'Do NOT write antecedents that require the opposite polarity or a '
            'different constant for these parameters.')

    if context.passthrough_overrides:
        lines.append('')
        lines.append(
            'Parameters set to a parent expression in every instance — keep '
            'properties parametric in these:')
        for name, value in sorted(context.passthrough_overrides.items()):
            lines.append(f'  - {name} = {value}')
        lines.append(
            'Do not hard-code a single value for these; prefer width- and '
            'count-agnostic wording.')

    if context.differing_overrides:
        lines.append('')
        lines.append(
            'Parameters that differ across instances — keep properties '
            'parametric:')
        for name, pairs in sorted(context.differing_overrides.items()):
            detail = '; '.join(f'{inst} uses {value}' for inst, value in pairs)
            lines.append(f'  - {name}: {detail}')
        lines.append(
            'Prefer width- and count-agnostic wording (e.g. |req_i, '
            '$onehot0(gnt_o)) over hard-coded widths taken from one instance.')

    if context.untouched_defaults:
        lines.append('')
        lines.append(
            'Parameters never overridden under this parent (module defaults '
            'apply):')
        for name, value in sorted(context.untouched_defaults.items()):
            lines.append(f'  - {name} = {value}')
        lines.append(
            'Do NOT write antecedents that require a different value for these '
            'parameters.')

    if context.resolved_elaboration_overrides:
        lines.append('')
        lines.append(
            'FPV elaborates this leaf with these parent compile-time '
            'constants (not the module-header defaults):')
        for name, value in sorted(
                context.resolved_elaboration_overrides.items()):
            lines.append(f'  - {name} = {value}')
        lines.append(
            'Do NOT write antecedents that require a different constant for '
            'these parameters.')

    lines.append('')
    lines.append(
        'Where a parameter only sets a width or count, keep the property '
        'parametric even when every instance agrees on the value.')
    return '\n'.join(lines)


def context_file_path(module_name: str, cwd: Optional[str] = None) -> str:
    """Path where the submodule flow stores prompt context for the agent."""
    root = cwd or os.getcwd()
    return os.path.join(root, f'ft_{module_name}', 'sva',
                        'instantiation_context.txt')


def write_context_file(
    context: ModuleInstantiationContext,
    cwd: Optional[str] = None,
) -> Optional[str]:
    """Write the prompt block for ``context.module``; return the path or None."""
    text = format_instantiation_prompt(context)
    if not text:
        return None
    path = context_file_path(context.module, cwd=cwd)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(text)
        handle.write('\n')
    return path


def read_context_file(
    module_name: str,
    cwd: Optional[str] = None,
) -> str:
    """Return stored instantiation context text, or '' if absent."""
    path = context_file_path(module_name, cwd=cwd)
    if not os.path.isfile(path):
        return ''
    with open(path, encoding='utf-8') as handle:
        return handle.read().strip()


def load_leaf_defaults(rtl_path: str) -> Dict[str, str]:
    """Read parameter defaults from a leaf RTL file path."""
    if not rtl_path or not os.path.isfile(rtl_path):
        return {}
    with open(rtl_path, encoding='utf-8', errors='replace') as handle:
        return extract_module_parameter_defaults(handle.read())


def build_contexts_for_parent(
    parent_rtl_path: str,
    submodule_files: Mapping[str, str],
    package_texts: Optional[Mapping[str, str]] = None,
) -> Dict[str, ModuleInstantiationContext]:
    """Extract and aggregate contexts for every located direct submodule."""
    if not os.path.isfile(parent_rtl_path):
        return {}
    with open(parent_rtl_path, encoding='utf-8', errors='replace') as handle:
        parent_text = handle.read()
    defaults = {
        name: load_leaf_defaults(path)
        for name, path in submodule_files.items()
    }
    contexts = collect_parent_contexts(parent_text, defaults)
    kept = {
        name: context
        for name, context in contexts.items()
        if name in submodule_files
    }
    for context in kept.values():
        context.resolved_elaboration_overrides = resolve_elaboration_overrides(
            context, parent_text, package_texts)
    return kept
