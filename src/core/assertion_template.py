"""Template-based SVA assertion assembly and structured LLM output parsing."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Tuple

DESIGNER_MARKER = '//====DESIGNER-ADDED-SVA====//'

TEMPLATE_RULES = '''
You are an expert in SystemVerilog Assertions (SVA) for RTL module MODULE.
Deliver assertions as structured entries only. Do NOT write SystemVerilog syntax.

For each assertion output exactly this block (separated by --- lines):
---
id: <short_snake_case_name>
property: <SVA expression only>
failure: <short failure message for simulation>

Rules for the id field:
- Use snake_case without any prefix (a_ is added automatically).

Rules for the property field:
- Expression only: no assert property, no semicolon, no @(posedge clk), and no
  disable iff (the checker template owns clock/reset control).
- Use |-> for same-cycle and |=> for next-cycle implications.
- Use the checker module's ports and parameters by their unqualified names.
  Do NOT write MODULE.signal: the checker is bound below the DUT and VC Formal
  cannot resolve that module-type path for assertion-density analysis.
- Do not reference DUT internals unless they are explicitly declared as checker
  inputs. Prefer the DUT interface over implementation detail.
- DO NOT use $past() in preconditions, only in postconditions.
- DO NOT use generate loops, within, or first_match.
- DO NOT apply bit-select or part-select to parenthesized expressions (e.g. avoid "(a + b)[127:0]").
- Prefer top-level module inputs/outputs; avoid low-level implementation detail.
- Signals ending in _q are registers (next-cycle behavior); wires update same-cycle.
- Prefer width- and count-agnostic properties (e.g. |req_i, $onehot0(gnt_o))
  over hard-coded widths taken from a single parameter value.
- When a PARENT INSTANTIATION CONTEXT block is present, target the parameter
  polarities and values it lists. Do NOT write antecedents that require a
  polarity or constant the parent never uses — those proofs are vacuous.

Rules for the failure field:
- One short plain-text message, no quotes needed in the field value.
- Describe what went wrong when the assertion fails.

Write assertions to check ALL functionality of MODULE except reset behavior.
Output ONLY assertion blocks, no commentary.
'''

STRUCTURED_OUTPUT_EXAMPLE = '''
---
id: req_implies_grnt
property: req_i |=> grnt_o
failure: Request asserted but grant not received next cycle
---
id: valid_stable
property: valid_i && !ready_i |=> valid_i && $stable(data_i)
failure: Valid dropped or data changed while waiting for ready
'''


@dataclass
class AssertionSpec:
    id: str
    property: str
    failure: str


def normalize_assertion_id(assertion_id: str) -> str:
    """Return a legal snake-case label with the ``a_`` prefix SVALint expects.

    Model output is not trusted to obey the prompt.  In particular, spaces,
    punctuation, non-ASCII text, and a leading digit would all turn the label
    into invalid SystemVerilog if copied verbatim.
    """
    name = assertion_id.strip()
    if name.startswith('as__'):
        name = name[4:]
    elif name.startswith('a_'):
        name = name[2:]
    elif name.startswith('__'):
        name = name[2:]

    # Transliterate where possible, split CamelCase, then collapse everything
    # that is not legal inside an identifier into one underscore.
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()
    name = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', name)
    name = re.sub(r'[^A-Za-z0-9_]+', '_', name).strip('_').lower()
    name = re.sub(r'_+', '_', name)
    if not name:
        name = 'assertion'
    if name[0].isdigit():
        name = 'assertion_' + name
    return f'a_{name}'


def escape_display_string(text: str) -> str:
    """Escape a string for use inside $display("...")."""
    return (
        text.replace('\\', '\\\\')
        .replace('"', '\\"')
        .replace('\n', '\\n')
        .replace('\r', '\\r')
        .replace('\t', '\\t')
    )


def strip_clocking_event(property_expr: str) -> str:
    """Drop a leading ``@(...)`` / ``disable iff (...)`` from a property body.

    Combinational checkers own clock and reset through default clocking. An
    explicit event inside ``assert property`` either names a DUT clock that
    does not exist or fights that default clocking block.
    """
    prop = property_expr.strip()
    changed = True
    while changed and prop:
        changed = False
        if prop.startswith('@'):
            opening = prop.find('(')
            if opening == -1:
                break
            closing = _matching_paren(prop, opening)
            if closing is None:
                break
            prop = prop[closing + 1:].strip()
            changed = True
            continue
        disable = re.match(r'disable\s+iff\s*\(', prop, re.IGNORECASE)
        if disable:
            closing = _matching_paren(prop, disable.end() - 1)
            if closing is None:
                break
            prop = prop[closing + 1:].strip()
            changed = True
            continue
        if prop.startswith('(') and prop.endswith(')'):
            inner = prop[1:-1].strip()
            if inner.startswith('@') or re.match(r'disable\s+iff\b', inner, re.I):
                prop = inner
                changed = True
    return prop


def rewrite_assertions_without_clock_events(text: str) -> str:
    """Rewrite every labelled assertion so its property body has no ``@(...)``."""
    masked = _mask_noncode(text)
    pieces = []
    last = 0
    for match in _ASSERTION_HEADER.finditer(masked):
        opening = masked.find('(', match.end())
        if opening == -1:
            continue
        closing = _matching_paren(text, opening)
        if closing is None:
            continue
        stripped = strip_clocking_event(text[opening + 1:closing])
        pieces.append(text[last:opening + 1])
        pieces.append(stripped)
        last = closing
    pieces.append(text[last:])
    return ''.join(pieces)


def validate_property_expression(property_expr: str) -> str:
    """Validate and return an expression-only property field.

    The template owns ``assert property``, clocking, and termination.  Allowing
    those tokens through from a model response defeats the purpose of templated
    generation and commonly produces nested or prematurely terminated source.
    """
    prop = property_expr.strip()
    if not prop:
        raise ValueError('property expression is empty')
    if re.search(r'\bassert\s+property\b', prop, re.IGNORECASE):
        raise ValueError('property must contain an expression only, not assert property')
    if ';' in prop:
        raise ValueError('property expression must not contain a semicolon')
    if re.search(r'@\s*\(', prop):
        raise ValueError('property expression must not contain an explicit clock event')
    if re.search(r'\bdisable\s+iff\b', prop, re.IGNORECASE):
        raise ValueError('property expression must not contain disable iff')
    return prop


def assemble_assertion(
    assertion_id: str,
    property_expr: str,
    failure_text: str,
    indent: str = '',
    module_type: str = 'sequential',
) -> str:
    """Build a lint-compliant assertion from structured fields."""
    name = normalize_assertion_id(assertion_id)
    if module_type == 'combinational':
        property_expr = strip_clocking_event(property_expr)
    prop = validate_property_expression(property_expr)
    if prop.startswith('(') and prop.endswith(')'):
        inner = prop
    else:
        inner = f'({prop})'
    failure = escape_display_string(failure_text.strip() or f'Assertion {name} failed')
    return (
        f'{indent}{name}: assert property ({inner})\n'
        f'{indent}else begin\n'
        f'{indent}    $display("{failure}");\n'
        f'{indent}end'
    )


def assemble_assertions(
    specs: List[AssertionSpec],
    indent: str = '',
    module_type: str = 'sequential',
) -> List[str]:
    return [
        assemble_assertion(
            s.id, s.property, s.failure, indent=indent, module_type=module_type)
        for s in specs
    ]


def parse_structured_assertions(text: str) -> List[AssertionSpec]:
    """Parse strict structured assertion blocks from model output.

    A response may be wrapped in a Markdown fence, and long values may continue
    on indented lines.  Unknown or duplicate fields reject only their own block.
    When delimiters are present, text before the first one is commentary and is
    never interpreted as an assertion.
    """
    specs: List[AssertionSpec] = []
    delimiter = re.compile(r'^\s*---\s*$', re.MULTILINE)
    matches = list(delimiter.finditer(text))
    if matches:
        blocks = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            blocks.append(text[match.end():end])
    else:
        blocks = [text]

    for block in blocks:
        block = re.sub(r'^\s*```[A-Za-z0-9_-]*\s*$', '', block,
                       flags=re.MULTILINE).strip()
        if not block:
            continue
        spec = _parse_block(block)
        if spec is not None:
            specs.append(spec)
    return specs


def _parse_block(block: str) -> Optional[AssertionSpec]:
    fields = {}
    current = None
    field_re = re.compile(r'^\s*(id|property|failure)\s*:\s*(.*)$',
                          re.IGNORECASE)
    unknown_field_re = re.compile(r'^\s*[A-Za-z_]\w*\s*:')

    for raw_line in block.splitlines():
        if not raw_line.strip():
            continue
        match = field_re.match(raw_line)
        if match:
            name = match.group(1).lower()
            if name in fields:
                return None
            fields[name] = match.group(2).strip()
            current = name
            continue
        if unknown_field_re.match(raw_line):
            return None
        if current is not None and raw_line[:1].isspace():
            continuation = raw_line.strip()
            if continuation:
                fields[current] = (fields[current] + ' ' + continuation).strip()
            continue
        # Undelimited prose must not become source merely because fields occur
        # somewhere later in it.
        return None

    if not fields.get('id') or not fields.get('property'):
        return None
    return AssertionSpec(
        id=fields['id'],
        property=fields['property'],
        failure=fields.get('failure', f"Assertion {fields['id']} failed"),
    )


def _trim_property_body(body: str) -> str:
    body = body.strip()
    while body.endswith(')') and body.count('(') < body.count(')'):
        body = body[:-1].rstrip()
    return body


def _mask_noncode(text: str) -> str:
    """Blank comments and string contents while preserving offsets/newlines."""
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
        elif state == 'string':
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


def _matching_paren(text: str, opening: int) -> Optional[int]:
    """Index of the parenthesis matching ``opening``, ignoring strings."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return index
    return None


_ASSERTION_HEADER = re.compile(
    r'^[ \t]*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*assert\s+property\b',
    re.IGNORECASE | re.MULTILINE,
)


def _assertion_extent(text: str, masked: str, match: re.Match,
                      limit: int) -> Optional[Tuple[int, int, str, bool]]:
    """Return ``(start, end, property, has_else)`` for one assertion."""
    opening = text.find('(', match.end(), limit)
    if opening == -1:
        return None
    closing = _matching_paren(text, opening)
    if closing is None or closing >= limit:
        return None

    prop = text[opening + 1:closing].strip()
    cursor = closing + 1
    while cursor < limit and text[cursor].isspace():
        cursor += 1
    if cursor < limit and text[cursor] == ';':
        return match.start(), cursor + 1, prop, False

    else_match = re.match(r'else\b', masked[cursor:limit], re.IGNORECASE)
    if not else_match:
        return None
    action_start = cursor + else_match.end()
    tail = masked[action_start:limit]
    begin_match = re.search(r'\bbegin\b', tail, re.IGNORECASE)
    if begin_match:
        token_re = re.compile(r'\b(begin|end)\b', re.IGNORECASE)
        depth = 0
        for token in token_re.finditer(masked, action_start + begin_match.start(), limit):
            if token.group(1).lower() == 'begin':
                depth += 1
            else:
                depth -= 1
                if depth == 0:
                    return match.start(), token.end(), prop, True
        return None

    semicolon = masked.find(';', action_start, limit)
    if semicolon == -1:
        return None
    return match.start(), semicolon + 1, prop, True


def _extract_assembled_assertions(
    response: str,
    module_type: str = 'sequential',
) -> List[str]:
    """Extract bounded legacy SVA blocks without copying surrounding prose."""
    # Some API clients hand back one physical line containing escaped line
    # breaks.  Decode that representation only when there are no real line
    # breaks; otherwise a deliberate ``\n`` inside $display must stay escaped.
    if '\n' not in response and '\\n' in response:
        response = response.replace('\\r\\n', '\n').replace('\\n', '\n')

    assertions: List[str] = []
    masked = _mask_noncode(response)
    matches = list(_ASSERTION_HEADER.finditer(masked))
    for index, match in enumerate(matches):
        limit = matches[index + 1].start() if index + 1 < len(matches) else len(response)
        extent = _assertion_extent(response, masked, match, limit)
        if extent is None:
            continue
        start, end, prop, has_else = extent
        name = normalize_assertion_id(match.group(1))
        body = _trim_property_body(prop)
        if module_type == 'combinational':
            body = strip_clocking_event(body)
        if has_else:
            try:
                validate_property_expression(body)
            except ValueError:
                continue
            block = rename_assertion(response[start:end], name).rstrip()
            if module_type == 'combinational':
                block = rewrite_assertions_without_clock_events(block)
            assertions.append(block)
            continue
        try:
            assertions.append(
                assemble_assertion(
                    name, body, f'Assertion {name} failed',
                    module_type=module_type))
        except ValueError:
            continue
    return assertions


def extract_assertions_from_response(
    response: str,
    module_type: str = 'sequential',
) -> List[str]:
    """Parse structured LLM output; fall back to assembled SVA blocks."""
    specs = parse_structured_assertions(response)
    if specs:
        assertions = []
        for spec in specs:
            try:
                assertions.append(
                    assemble_assertion(
                        spec.id, spec.property, spec.failure,
                        module_type=module_type))
            except ValueError:
                # One malformed model entry must not discard valid siblings or
                # leak its forbidden wrapper into generated source.
                continue
        return assertions
    if re.search(r'^\s*---\s*$', response, re.MULTILINE) and re.search(
            r'^\s*(?:id|property|failure)\s*:', response,
            re.IGNORECASE | re.MULTILINE):
        return []
    return _extract_assembled_assertions(response, module_type=module_type)


def get_assertion_name(assertion: str) -> Optional[str]:
    match = _ASSERTION_HEADER.search(_mask_noncode(assertion))
    return match.group(1) if match else None


def assertion_property_span(assertion: str) -> Optional[Tuple[int, int]]:
    """Return the source span occupied by a labelled assertion's expression.

    The returned half-open ``(start, end)`` range excludes the parentheses owned
    by ``assert property``. Comments and strings cannot masquerade as a header.
    This lets downstream preprocessing modify only the property expression,
    never its label, action block, comments outside the property, or failure
    message.
    """
    masked = _mask_noncode(assertion)
    header = _ASSERTION_HEADER.search(masked)
    if header is None:
        return None
    opening = masked.find('(', header.end())
    if opening == -1:
        return None
    closing = _matching_paren(assertion, opening)
    if closing is None:
        return None
    return opening + 1, closing


def assertion_property_text(assertion: str) -> Optional[str]:
    """The assertion reduced to 'label: assert property (body);'.

    The failure action, trailing comments and anything else around the property
    are not part of the property and have no business reaching the parser, which
    converts the body to clauses.  Because the span is found on masked source, a
    failure message mentioning 'assert property' cannot stand in for the real
    one.  None means the text is not a labelled assertion.
    """
    span = assertion_property_span(assertion)
    if span is None:
        return None
    name = get_assertion_name(assertion)
    label = f'{name}: ' if name else ''
    return f'{label}assert property ({assertion[span[0]:span[1]]});'


def split_designer_assertion_blocks(contents: str) -> List[str]:
    """Split the designer-added section into individual assertion blocks."""
    marker = DESIGNER_MARKER
    if marker not in contents:
        return []

    designer_section = contents.split(marker, 1)[1]
    masked = _mask_noncode(designer_section)
    endmodule = re.search(r'^[ \t]*endmodule\b', masked,
                          re.IGNORECASE | re.MULTILINE)
    if endmodule:
        designer_section = designer_section[:endmodule.start()]
    return _extract_assembled_assertions(designer_section)


def process_llm_output_to_sv(text: str, module_type: str = 'sequential') -> str:
    """Convert structured LLM output to assembled SystemVerilog assertions."""
    assertions = extract_assertions_from_response(text, module_type=module_type)
    return '\n\n'.join(assertions)


def rename_assertion(assertion: str, new_name: str) -> str:
    name = normalize_assertion_id(new_name)
    return re.sub(
        r'^([ \t]*)([a-zA-Z_][a-zA-Z0-9_]*)(\s*:\s*assert\s+property)',
        lambda match: f'{match.group(1)}{name}{match.group(3)}',
        assertion,
        count=1,
        flags=re.IGNORECASE | re.MULTILINE,
    )
