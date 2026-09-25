"""RTL mutation engine for mutation-validated snapshot evaluation.

Bug-injection anecdotes cannot show that a snapshot detects unintended RTL
change; a systematic mutant population can.  This module generates deterministic
single-point mutations of a SystemVerilog module across the six fault classes
that matter for regression sensitivity:

=================  ============================================================
``control``        Branch and condition faults: inverted guards, swapped
                   boolean connectives, altered relational operators.
``datapath``       Arithmetic and bit-manipulation faults: swapped operators,
                   off-by-one constants, shifted bit indices.
``timing``         Blocking/non-blocking swaps and clock-edge changes that move
                   behaviour by a cycle.
``reset``          Reset polarity and reset-value faults, and dropped resets.
``interface``      Port-connection faults on submodule instances and outputs
                   tied to constants.
``state_update``   Next-state faults: held state, skipped transitions, wrong
                   target state.
=================  ============================================================

Two properties make the resulting population usable as a benchmark:

*Uniqueness* — mutants are deduplicated on a whitespace- and comment-normalised
hash of the mutated source, so the same effective edit is never counted twice.

*Non-equivalence* — a mutant that is logically equivalent to the original cannot
be detected by any correct property and would unfairly depress a detection rate.
:func:`screen_non_equivalence` proves or refutes equivalence with the formal tool
using a generated miter, and mutants proved equivalent are excluded from the
population rather than counted as escapes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from coi import strip_sv_noise, split_module_header, is_declaration


class FaultClass(str, Enum):
    CONTROL = 'control'
    DATAPATH = 'datapath'
    TIMING = 'timing'
    RESET = 'reset'
    INTERFACE = 'interface'
    STATE_UPDATE = 'state_update'


class EquivalenceVerdict(str, Enum):
    #: Proved different: the mutant is a valid benchmark member.
    NON_EQUIVALENT = 'non_equivalent'
    #: Proved identical: excluded from the population.
    EQUIVALENT = 'equivalent'
    #: The equivalence check did not conclude (timeout, unsupported hierarchy).
    UNKNOWN = 'unknown'
    #: Screening was not requested.
    NOT_SCREENED = 'not_screened'


@dataclass
class Mutant:
    """A single-point mutation of one RTL module."""

    mutant_id: str
    fault_class: FaultClass
    operator: str
    line_number: int
    original_line: str
    mutated_line: str
    source: str = field(repr=False, default='')
    fingerprint: str = ''
    equivalence: EquivalenceVerdict = EquivalenceVerdict.NOT_SCREENED
    #: Populated by the evaluation harness.
    detected_by: List[str] = field(default_factory=list)
    escape_reason: str = ''

    @property
    def detected(self) -> bool:
        return bool(self.detected_by)

    def description(self) -> str:
        return (
            f'{self.fault_class.value}/{self.operator} at line {self.line_number}: '
            f'{self.original_line.strip()!r} -> {self.mutated_line.strip()!r}'
        )

    def to_dict(self, include_source: bool = False) -> dict:
        data = asdict(self)
        data['fault_class'] = self.fault_class.value
        data['equivalence'] = self.equivalence.value
        data['detected'] = self.detected
        data['description'] = self.description()
        if not include_source:
            data.pop('source', None)
        return data


_COMMENT_RE = re.compile(r'/\*.*?\*/|//[^\n]*', re.DOTALL)


def normalise_source(text: str) -> str:
    """Collapse a source file to a comparison canonical form.

    Comments and all whitespace runs are removed so that two mutations producing
    the same effective code — for example changing ``a&&b`` and ``a  &&  b`` in
    equivalent ways — share one fingerprint.  Literals are deliberately kept:
    two mutants that differ only in a constant are different faults.
    """
    return re.sub(r'\s+', '', _COMMENT_RE.sub(' ', text))


def fingerprint_source(text: str) -> str:
    return hashlib.sha256(normalise_source(text).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Line classification
# ---------------------------------------------------------------------------

_ALWAYS_FF_RE = re.compile(r'\balways_ff\b|\balways\s*@\s*\(\s*posedge|\balways\s*@\s*\(\s*negedge')
_RESET_HINT_RE = re.compile(r'\b(?:rst|rstn|reset|resetn|nrst|arst)\w*\b', re.IGNORECASE)
_CLOCK_HINT_RE = re.compile(r'\b(?:clk|clock)\w*\b', re.IGNORECASE)
_INSTANCE_RE = re.compile(
    r'^\s*(?P<module>[A-Za-z_]\w*)\s*(?:#\s*\([^;]*?\)\s*)?(?P<inst>[A-Za-z_]\w*)\s*\(',
)


def _is_comment_or_blank(line: str) -> bool:
    stripped = line.strip()
    return (
        not stripped
        or stripped.startswith('//')
        or stripped.startswith('*')
        or stripped.startswith('/*')
    )


def split_trailing_comment(line: str) -> Tuple[str, str]:
    """Split a line into its code part and its trailing comment.

    Mutating comment text produces a diff that changes nothing, or — when the
    comment is a block terminator — a source file that no longer parses, so
    operators are only ever applied to the code part.
    """
    in_string = False
    for index in range(len(line) - 1):
        char = line[index]
        if char == '"' and (index == 0 or line[index - 1] != '\\'):
            in_string = not in_string
        elif not in_string and line[index:index + 2] in ('//', '/*'):
            return line[:index], line[index:]
    return line, ''


def block_comment_lines(lines: Sequence[str]) -> List[bool]:
    """Mark lines that sit inside a ``/* ... */`` block comment."""
    inside = [False] * len(lines)
    open_block = False
    for index, line in enumerate(lines):
        if open_block:
            inside[index] = True
            if '*/' in line:
                open_block = False
            continue
        stripped = line.strip()
        if '/*' in line and '*/' not in line.split('/*', 1)[1]:
            open_block = True
            inside[index] = stripped.startswith('/*')
    return inside


#: Line prefixes that introduce declarations rather than behaviour.  Mutating
#: them yields compile errors or type changes instead of functional faults.
_NON_BEHAVIOURAL_RE = re.compile(
    r'^\s*(?:'
    r'`\w+'                              # compiler directives
    r'|import\b|export\b|package\b|endpackage\b'
    r'|typedef\b|struct\b|union\b|enum\b'
    r'|parameter\b|localparam\b|genvar\b'
    r'|module\b|endmodule\b|interface\b|modport\b'
    r'|function\b|endfunction\b|task\b|endtask\b'
    r')'
)


#: ANSI, Verilog-95 body, and function/task ports. Width edits here are type
#: changes, not datapath faults.
_PORT_DECL_RE = re.compile(r'^\s*(?:input|output|inout)\b')


def _is_mutable_line(line: str) -> bool:
    """True when a line carries behaviour that a fault can meaningfully change."""
    if _is_comment_or_blank(line):
        return False
    if _NON_BEHAVIOURAL_RE.match(line):
        return False
    if _PORT_DECL_RE.match(line):
        return False
    # Enum body entries (bare labels inside a typedef block) have no operators
    # to mutate and only produce type changes.
    if re.fullmatch(r'\s*[A-Za-z_]\w*\s*,?\s*', line):
        return False
    # Signal declarations only carry widths and types; mutating them changes the
    # structure of the design rather than injecting a functional fault.
    return not is_declaration(line)


#: Constructs that belong to the verification code embedded in the RTL rather
#: than to the design. Mutating them injects a fault into the checking code, not
#: into the hardware, so the resulting "detection" would be meaningless.
_VERIFICATION_RE = re.compile(
    r'\b(?:assert|assume|cover|expect|restrict)\s+(?:property|final)?\b'
    r'|\bdefault\s+disable\s+iff\b'
    r'|\bdefault\s+clocking\b'
    r'|\$(?:error|fatal|warning|info|display|assert\w*)\b'
    r'|\bproperty\b\s*\w*\s*[;(]'
    r'|\bsequence\b\s*\w*\s*[;(]'
)

_TRANSLATE_OFF_RE = re.compile(
    r'(?:pragma\s+)?(?:synopsys|synthesis|verilator|translate|pragma)\s*'
    r'(?:translate_off|coverage_off|lint_off\s+\w+)?', re.IGNORECASE)


def verification_regions(lines: Sequence[str]) -> List[bool]:
    """Mark the lines that belong to verification or simulation-only code.

    Two things are excluded: RTL-embedded SVA (mutating an assertion tells us
    nothing about the snapshot) and everything between ``translate_off`` and
    ``translate_on`` pragmas, which never reaches synthesis.
    """
    excluded = [False] * len(lines)
    off = False
    depth_guard = 0

    for index, line in enumerate(lines):
        lowered = line.lower()
        if 'translate_off' in lowered or 'synthesis off' in lowered:
            off = True
        if off:
            excluded[index] = True
            if 'translate_on' in lowered or 'synthesis on' in lowered:
                off = False
            continue

        if depth_guard > 0:
            excluded[index] = True
            depth_guard += line.count('(') - line.count(')')
            if depth_guard <= 0 or line.rstrip().endswith(';'):
                depth_guard = 0
            continue

        if _VERIFICATION_RE.search(line):
            excluded[index] = True
            # A multi-line assertion keeps its continuation lines excluded too.
            depth_guard = max(0, line.count('(') - line.count(')'))

    return excluded


# ---------------------------------------------------------------------------
# Elaboration reachability
# ---------------------------------------------------------------------------
#
# A module is mutated as source, but it is verified as an *elaborated* instance.
# Code inside a conditional generate branch that the elaboration parameters
# switch off is not part of the design under proof, so a fault injected there
# cannot be detected by any property.  Counting such a mutant as an escape would
# understate the snapshot; the lines are therefore excluded up front.

_SV_SIZED_LITERAL_RE = re.compile(r"(?:\d+)?'[sS]?[bodhBODH]([0-9a-fA-FxXzZ_]+)")
_UNSIZED_LITERAL_RE = re.compile(r"'([01])")
_CAST_RE = re.compile(r"\b\w+\s*'\s*\(")
_TERNARY_RE = re.compile(r'([^?]+)\?([^:]+):(.+)')

_LITERAL_BASE = {'b': 2, 'o': 8, 'd': 10, 'h': 16}


def _sv_literal_to_int(match: re.Match) -> str:
    text = match.group(0)
    base_char = re.search(r"'[sS]?([bodhBODH])", text)
    digits = match.group(1).replace('_', '')
    if not base_char or not digits or re.search(r'[xXzZ]', digits):
        return 'None'
    try:
        return str(int(digits, _LITERAL_BASE[base_char.group(1).lower()]))
    except ValueError:
        return 'None'


def _to_python_expression(expr: str) -> Optional[str]:
    """Translate a constant SystemVerilog expression into Python syntax.

    Returns ``None`` when the expression uses constructs this translator cannot
    evaluate safely, in which case the caller must treat the condition as
    unknown and leave the branch enabled.
    """
    text = expr.strip()
    if not text:
        return None

    text = _SV_SIZED_LITERAL_RE.sub(_sv_literal_to_int, text)
    text = _UNSIZED_LITERAL_RE.sub(r'\1', text)
    # `unsigned'(x)`, `idx_t'(x)` and friends are width casts: drop the cast and
    # keep the value, which is what matters for a constant comparison.
    text = _CAST_RE.sub('(', text)
    text = re.sub(r'\$clog2\s*\(', 'clog2(', text)
    text = re.sub(r'\$bits\s*\(', 'bits(', text)

    ternary = _TERNARY_RE.match(text)
    if ternary:
        condition, true_value, false_value = ternary.groups()
        inner = [_to_python_expression(part) for part in (true_value, false_value, condition)]
        if any(part is None for part in inner):
            return None
        text = f'(({inner[0]}) if ({inner[2]}) else ({inner[1]}))'
    else:
        text = text.replace('&&', ' and ').replace('||', ' or ')
        text = re.sub(r'!(?!=)', ' not ', text)

    if re.search(r'[{}@#$"]|\+\+|--|<<<|>>>', text):
        return None
    return text


def _clog2(value) -> int:
    value = int(value)
    return 0 if value <= 1 else (value - 1).bit_length()


def evaluate_constant(expr: str, parameters: Dict[str, int]) -> Optional[int]:
    """Evaluate a constant SystemVerilog expression, or return ``None``.

    ``None`` means "not statically known"; callers must keep the associated code
    live rather than guessing.
    """
    python_expr = _to_python_expression(expr)
    if python_expr is None:
        return None

    names = set(re.findall(r'\b[A-Za-z_]\w*\b', python_expr))
    allowed = {'and', 'or', 'not', 'if', 'else', 'clog2', 'bits', 'True', 'False'}
    if names - allowed - set(parameters):
        return None

    environment = dict(parameters)
    environment.update({'clog2': _clog2, 'bits': lambda value: int(value).bit_length()})
    try:
        value = eval(python_expr, {'__builtins__': {}}, environment)  # noqa: S307
    except Exception:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return None


def module_parameters(rtl_text: str) -> Dict[str, int]:
    """Extract the default values of a module's integer parameters.

    Only parameters whose defaults evaluate to a constant are returned; type
    parameters and expressions this module cannot evaluate are skipped, and any
    condition depending on them is treated as unknown.
    """
    # strip_sv_noise() also deletes sized literals, which are exactly the
    # parameter defaults being read here, so only comments are removed.
    text = _COMMENT_RE.sub('', rtl_text)
    _, header, _, _ = split_module_header(text)

    parameters: Dict[str, int] = {}
    # Parameters are order-dependent (a later one may use an earlier one), so
    # they are evaluated in declaration order against what is known so far.
    for match in re.finditer(
            r'\b(?:parameter|localparam)\b[^;,)]*?\b(?P<name>[A-Za-z_]\w*)\s*=\s*'
            r'(?P<value>[^,;)]+)', header or ''):
        value = evaluate_constant(match.group('value'), parameters)
        if value is not None:
            parameters[match.group('name')] = value

    body = text.split(')', 1)[-1]
    for match in re.finditer(
            r'^\s*localparam\b[^;=]*?\b(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<value>[^;]+);',
            body, re.MULTILINE):
        value = evaluate_constant(match.group('value'), parameters)
        if value is not None:
            parameters.setdefault(match.group('name'), value)

    return parameters


_IF_BEGIN_RE = re.compile(
    r'(?:^\s*|\belse\s+)if\s*\((?P<cond>.*)\)\s*begin\b(?:\s*:\s*\w+)?\s*$')
_BLOCK_TOKEN_RE = re.compile(r'\bbegin\b|\bend\b')


def _match_begin_end(lines: Sequence[str]) -> Dict[Tuple[int, int], Tuple[int, int]]:
    """Map every ``begin`` to the ``end`` that closes it.

    Positions are ``(line, column)`` because ``end else begin`` puts a closing
    and an opening token on the same line, and a line-granular counter cannot
    tell them apart.
    """
    matches: Dict[Tuple[int, int], Tuple[int, int]] = {}
    stack: List[Tuple[int, int]] = []
    for line_number, line in enumerate(lines):
        for token in _BLOCK_TOKEN_RE.finditer(line):
            if token.group(0) == 'begin':
                stack.append((line_number, token.start()))
            elif stack:
                matches[stack.pop()] = (line_number, token.start())
    return matches


def dead_generate_lines(
    lines: Sequence[str],
    parameters: Dict[str, int],
) -> List[bool]:
    """Mark lines inside conditional branches that the parameters switch off.

    Only branches with a statically decidable condition are pruned, so the
    filter can never drop a line the tool would have elaborated.  Conditions
    that depend on run-time signals evaluate to "unknown" and keep both
    branches live.
    """
    dead = [False] * len(lines)
    matches = _match_begin_end(lines)
    processed: Set[int] = set()

    def begin_after(line_number: int, column: int) -> Optional[int]:
        for token in _BLOCK_TOKEN_RE.finditer(lines[line_number]):
            if token.group(0) == 'begin' and token.start() >= column:
                return token.start()
        return None

    def mark(start: int, stop: int) -> None:
        for line_number in range(max(start, 0), min(stop, len(lines))):
            dead[line_number] = True

    def process(line_number: int, parent_alive: bool) -> None:
        processed.add(line_number)
        match = _IF_BEGIN_RE.search(lines[line_number])
        if not match:
            return

        column = begin_after(line_number, match.start())
        if column is None or (line_number, column) not in matches:
            return
        end_line, end_column = matches[(line_number, column)]

        value = evaluate_constant(match.group('cond'), parameters)
        body_alive = parent_alive and (value is None or bool(value))
        else_alive = parent_alive and (value is None or not bool(value))

        if not body_alive:
            mark(line_number + 1, end_line)

        rest = lines[end_line][end_column:]
        if not re.search(r'\belse\b', rest):
            return

        if _IF_BEGIN_RE.search(rest):
            if not else_alive:
                dead[end_line] = True
            process(end_line, else_alive)
            return

        else_column = begin_after(end_line, end_column)
        if else_column is None or (end_line, else_column) not in matches:
            return
        else_end, _ = matches[(end_line, else_column)]
        if not else_alive:
            mark(end_line + 1, else_end)

    for line_number in range(len(lines)):
        if line_number in processed:
            continue
        if _IF_BEGIN_RE.search(lines[line_number]):
            process(line_number, True)

    return dead


_BUG_IFDEF_RE = re.compile(r'^\s*`if(n)?def\s+BUG\w*\b', re.IGNORECASE)


def ifdef_bug_dead_lines(lines: Sequence[str]) -> List[bool]:
    """Mark the branch of an `` `ifdef BUG_* `` that golden RTL does not compile.

    Several benchmarks (the 2605.06434 ALU among them) keep injected faults
    behind `` `ifndef BUG_n `` / `` `else ``. Those `` `else `` bodies are not
    in the elaborated golden design, so a mutant there cannot be detected.
    ``BUG_*`` is assumed undefined, matching a default compile.
    """
    dead = [False] * len(lines)
    index = 0
    while index < len(lines):
        match = _BUG_IFDEF_RE.match(lines[index])
        if not match:
            index += 1
            continue
        ifndef = match.group(1) == 'n'
        first_branch_dead = not ifndef
        index += 1
        in_else = False
        while index < len(lines) and not re.match(r'^\s*`endif\b', lines[index]):
            if re.match(r'^\s*`else\b', lines[index]):
                in_else = True
                index += 1
                continue
            dead[index] = (
                (not in_else and first_branch_dead)
                or (in_else and not first_branch_dead)
            )
            index += 1
        index += 1
    return dead


_CONDITION_RE = re.compile(r'\bif\s*\((?P<cond>.*)\)\s*(?:begin\b|$)')


def elaboration_equivalent(
    original_line: str,
    mutated_line: str,
    parameters: Dict[str, int],
) -> bool:
    """Report whether a mutated condition decides the same way at elaboration.

    Changing ``if (NumIn == 1)`` to ``if (NumIn == 2)`` looks like a fault, but
    with ``NumIn = 4`` both conditions are false and the elaborated netlist is
    identical.  Such a mutant can never be detected, and leaving it in the
    population depresses the detection rate for no reason, so it is dropped
    before any formal time is spent on it.

    Conditions that are not statically decidable — anything touching a signal —
    return ``False``, because those mutants describe real run-time faults.
    """
    original = _CONDITION_RE.search(_COMMENT_RE.sub('', original_line))
    mutated = _CONDITION_RE.search(_COMMENT_RE.sub('', mutated_line))
    if not original or not mutated:
        return False

    before = evaluate_constant(original.group('cond'), parameters)
    after = evaluate_constant(mutated.group('cond'), parameters)
    if before is None or after is None:
        return False
    return bool(before) == bool(after)


def _module_decl_offset(rtl_text: str) -> Optional[int]:
    """Byte offset of the real ``module`` keyword, ignoring comments.

    A comment such as `` `module name` `` must not start the header scan:
    that would leave the ANSI port list in the "body" and turn widths into
    fake datapath mutants.
    """
    in_block = False
    pos = 0
    for line in rtl_text.splitlines(keepends=True):
        if in_block:
            if '*/' in line:
                in_block = False
            pos += len(line)
            continue
        code, _ = split_trailing_comment(line)
        stripped = code.strip()
        if stripped.startswith('/*') and '*/' not in stripped[2:]:
            in_block = True
            pos += len(line)
            continue
        if stripped.startswith('//') or stripped.startswith('/*'):
            pos += len(line)
            continue
        match = re.search(r'\bmodule\s+\w+', code)
        if match:
            return pos + match.start()
        pos += len(line)
    return None


def _module_body_start_line(rtl_text: str) -> int:
    """Return the 0-based line index where the module body begins.

    Port declarations describe the interface, not behaviour, so mutations start
    after the header.  The scan runs on the original text (not the
    comment-stripped copy) so line numbers stay aligned with the source file.
    """
    start = _module_decl_offset(rtl_text)
    if start is None:
        return 0

    depth = 0
    seen_paren = False
    for index in range(start, len(rtl_text)):
        char = rtl_text[index]
        if char == '(':
            depth += 1
            seen_paren = True
        elif char == ')':
            depth -= 1
        elif char == ';' and depth == 0:
            return rtl_text[:index].count('\n') + 1
        if seen_paren and depth == 0 and char == ')':
            semicolon = rtl_text.find(';', index)
            if semicolon != -1:
                return rtl_text[:semicolon].count('\n') + 1
    return 0


def _in_reset_context(lines: Sequence[str], index: int, lookback: int = 6) -> bool:
    """True when a line sits inside an ``if (!rst)``-style reset branch."""
    for offset in range(1, lookback + 1):
        candidate = index - offset
        if candidate < 0:
            break
        text = lines[candidate]
        if _RESET_HINT_RE.search(text) and re.search(r'\bif\b', text):
            return True
        if re.search(r'\belse\b', text):
            return False
    return False


# ---------------------------------------------------------------------------
# Mutation operators
# ---------------------------------------------------------------------------
#
# Each operator is a callable ``(line) -> list[(mutated_line, operator_name)]``.
# Operators never mutate more than one location per returned variant, keeping
# every mutant a single-point fault.


def _replace_nth(text: str, pattern: str, replacement: str, occurrence: int) -> Optional[str]:
    """Replace only the ``occurrence``-th (0-based) regex match."""
    matches = list(re.finditer(pattern, text))
    if occurrence >= len(matches):
        return None
    match = matches[occurrence]
    return text[:match.start()] + replacement + text[match.end():]


def _swap_all_occurrences(line: str, pattern: str, replacement: str, operator: str):
    """Yield one variant per occurrence of ``pattern``."""
    variants = []
    for index in range(len(list(re.finditer(pattern, line)))):
        mutated = _replace_nth(line, pattern, replacement, index)
        if mutated and mutated != line:
            variants.append((mutated, operator))
    return variants


def _balanced_paren_close(line: str, open_index: int) -> Optional[int]:
    """Index of the ``)`` that matches ``line[open_index]``, or ``None``."""
    depth = 0
    for index in range(open_index, len(line)):
        char = line[index]
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return index
    return None


def _comparison_spans(line: str) -> List[Tuple[int, int]]:
    """Byte ranges where ``<=`` is a comparator, not a non-blocking assign.

    ``if (!rst) wp <= #1 0;`` contains both an ``if`` and an NBA. Replacing
    the assignment ``<=`` yields illegal Verilog (``wp < #1``).
    """
    spans: List[Tuple[int, int]] = []
    for match in re.finditer(r'\b(?:if|while|for)\s*\(', line):
        open_index = line.index('(', match.start())
        close_index = _balanced_paren_close(line, open_index)
        if close_index is not None:
            spans.append((open_index + 1, close_index))
    question = line.find('?')
    if question != -1:
        spans.append((0, question))
    return spans


def _swap_occurrences_in_spans(
    line: str,
    pattern: str,
    replacement: str,
    operator: str,
    spans: Sequence[Tuple[int, int]],
):
    """Like :func:`_swap_all_occurrences` but only inside ``spans``."""
    variants = []
    for start, end in spans:
        fragment = line[start:end]
        for index in range(len(list(re.finditer(pattern, fragment)))):
            mutated_fragment = _replace_nth(fragment, pattern, replacement, index)
            if mutated_fragment and mutated_fragment != fragment:
                variants.append((line[:start] + mutated_fragment + line[end:], operator))
    return variants


def _control_mutations(line: str):
    variants = []

    # Swap boolean connectives.
    variants += _swap_all_occurrences(line, r'&&', '||', 'and_to_or')
    variants += _swap_all_occurrences(line, r'(?<![|])\|\|(?![|])', '&&', 'or_to_and')

    # Alter relational and equality operators.
    comparison_spans = _comparison_spans(line)
    for pattern, replacement, name in (
        (r'(?<![<>=!])==(?!=)', '!=', 'eq_to_neq'),
        (r'!=(?!=)', '==', 'neq_to_eq'),
        (r'(?<![<>=!])<=(?=[^=])', '<', 'le_to_lt'),
        (r'(?<![<>=!])>=', '>', 'ge_to_gt'),
        (r'(?<![<>=!-])<(?![=<])', '<=', 'lt_to_le'),
        (r'(?<![<>=!-])>(?![=>])', '>=', 'gt_to_ge'),
    ):
        # `<=` is also non-blocking assignment. Only rewrite it inside an
        # ``if``/``while``/``for`` condition (or a ternary predicate).
        if name == 'le_to_lt':
            variants += _swap_occurrences_in_spans(
                line, pattern, replacement, name, comparison_spans)
            continue
        variants += _swap_all_occurrences(line, pattern, replacement, name)

    # Invert a branch guard.
    guard = re.search(r'\bif\s*\(', line)
    if guard:
        open_index = line.index('(', guard.start())
        depth = 0
        close_index = None
        for index in range(open_index, len(line)):
            if line[index] == '(':
                depth += 1
            elif line[index] == ')':
                depth -= 1
                if depth == 0:
                    close_index = index
                    break
        if close_index is not None:
            condition = line[open_index + 1:close_index]
            if condition.strip():
                inverted = (
                    condition.strip()[1:]
                    if condition.strip().startswith('!')
                    else f'!({condition.strip()})'
                )
                variants.append((
                    line[:open_index + 1] + inverted + line[close_index:],
                    'invert_branch_guard',
                ))
    return variants


def _datapath_mutations(line: str):
    variants = []

    for pattern, replacement, name in (
        (r'(?<![+\-*/&|^<>=!])\+(?![+=])', '-', 'add_to_sub'),
        (r'(?<![+\-*/&|^<>=!])-(?![-=>])', '+', 'sub_to_add'),
        (r'(?<![*])\*(?![*=])', '/', 'mul_to_div'),
        (r'(?<![/])/(?![/=*])', '*', 'div_to_mul'),
        (r'(?<![&])&(?![&=])', '|', 'bitand_to_bitor'),
        (r'(?<![|])\|(?![|=])', '&', 'bitor_to_bitand'),
        (r'<<(?!=)', '>>', 'shl_to_shr'),
        (r'>>(?!=)', '<<', 'shr_to_shl'),
    ):
        if name == 'mul_to_div' and re.search(r'@\s*\(\s*\*\s*\)', line):
            continue
        variants += _swap_all_occurrences(line, pattern, replacement, name)

    # Off-by-one on unsized decimal constants that are not part of an identifier
    # or a sized literal.
    for index, match in enumerate(re.finditer(r"(?<![\w'.])(\d+)(?![\w'])", line)):
        value = int(match.group(1))
        if value > 1_000_000:
            continue
        mutated = _replace_nth(
            line, r"(?<![\w'.])\d+(?![\w'])", str(value + 1), index
        )
        if mutated and mutated != line:
            variants.append((mutated, 'const_off_by_one'))

    # Shift a bit-select index by one.
    for index, match in enumerate(re.finditer(r'\[\s*(\d+)\s*\]', line)):
        value = int(match.group(1))
        mutated = _replace_nth(line, r'\[\s*\d+\s*\]', f'[{value + 1}]', index)
        if mutated and mutated != line:
            variants.append((mutated, 'bit_index_shift'))

    return variants


def sequential_block_extents(lines: Sequence[str]) -> List[Optional[Tuple[int, int]]]:
    """For every line, the extent of the clocked ``always`` block containing it.

    Needed because the observable effect of a blocking assignment depends on the
    rest of its block, not on the statement alone.
    """
    closing = _match_begin_end(lines)
    extents: List[Optional[Tuple[int, int]]] = [None] * len(lines)
    for start, line in enumerate(lines):
        if not _ALWAYS_FF_RE.search(line):
            continue
        opening = re.search(r'\bbegin\b', line)
        if opening:
            end = closing.get((start, opening.start()), (len(lines) - 1, 0))[0]
        else:
            # A single-statement block: it ends at the first semicolon.
            end = start
            while end < len(lines) - 1 and ';' not in lines[end]:
                end += 1
        for index in range(start, min(end, len(lines) - 1) + 1):
            if extents[index] is None:
                extents[index] = (start, end)
    return extents


def _base_signal(reference: str) -> str:
    """The bare signal name of an assignment target, without any selection."""
    return re.sub(r'\[.*', '', reference).strip().split()[-1] if reference.strip() else ''


def scheduling_equivalent(
    lines: Sequence[str],
    index: int,
    extent: Optional[Tuple[int, int]],
) -> bool:
    """True if rewriting ``<=`` as ``=`` on this line cannot change the design.

    Inside a clocked block the two forms differ only when the assigned signal is
    read again in the same block: that is where the immediate update becomes
    observable. With no such read the elaborated logic is identical, so the
    mutant is an equivalent one and reporting it as a survivor would understate
    what the properties actually miss.
    """
    if extent is None:
        return False
    lhs, operator, _ = _split_assignment(split_trailing_comment(lines[index])[0])
    if lhs is None or operator != '<=':
        return False
    target = _base_signal(lhs)
    if not target:
        return False

    reference = re.compile(rf'\b{re.escape(target)}\b')
    first, last = extent
    for line_number in range(first, min(last, len(lines) - 1) + 1):
        code, _ = split_trailing_comment(lines[line_number])
        if line_number == first:
            # The sensitivity list names clock and reset, not the data path.
            continue
        assigned, _, source = _split_assignment(code)
        if assigned is not None and reference.search(assigned):
            if reference.search(source):
                return False  # reads itself, so the update order is visible
            continue
        if reference.search(code):
            return False  # read by a condition or by another statement
    return True


def _timing_mutations(
    line: str,
    in_sequential_block: bool,
    lines: Sequence[str] = (),
    index: int = -1,
    extent: Optional[Tuple[int, int]] = None,
):
    variants = []

    if in_sequential_block:
        # Non-blocking to blocking changes when the update is observed. Only the
        # statement-level `<=` qualifies: inside an expression it is a
        # less-than-or-equal comparison, and rewriting it produces invalid code.
        lhs, operator, _ = _split_assignment(line)
        if (lhs is not None and operator == '<=' and '(' not in lhs
                and not (index >= 0 and scheduling_equivalent(lines, index, extent))):
            variants.append((
                re.sub(r'(?<![<>=!])<=(?!=)', '=', line, count=1),
                'nonblocking_to_blocking',
            ))

    if re.search(r'\balways_ff\b|\balways\s*@', line):
        if 'posedge' in line:
            variants.append((line.replace('posedge', 'negedge', 1), 'posedge_to_negedge'))
        elif 'negedge' in line and not _RESET_HINT_RE.search(line):
            variants.append((line.replace('negedge', 'posedge', 1), 'negedge_to_posedge'))

    # Move a $past reference by one cycle.
    for index, match in enumerate(re.finditer(r'\$past\s*\(([^,)]+)\)', line)):
        mutated = _replace_nth(
            line, r'\$past\s*\(([^,)]+)\)', f'$past({match.group(1)}, 2)', index
        )
        if mutated and mutated != line:
            variants.append((mutated, 'past_depth_shift'))

    return variants


def _reset_mutations(line: str, lines: Sequence[str], index: int):
    variants = []

    # Reset polarity in the sensitivity list or the guard.
    if _RESET_HINT_RE.search(line):
        if re.search(r'\bif\s*\(\s*!', line):
            variants.append((
                re.sub(r'\bif\s*\(\s*!', 'if (', line, count=1),
                'reset_polarity_active_high',
            ))
        elif re.search(r'\bif\s*\(\s*(?:rst|reset)\w*', line, re.IGNORECASE):
            variants.append((
                re.sub(r'\bif\s*\(\s*', 'if (!', line, count=1),
                'reset_polarity_active_low',
            ))

        if 'negedge' in line:
            variants.append((line.replace('negedge', 'posedge', 1), 'reset_edge_flip'))
        elif 'posedge' in line and re.search(r'posedge\s+(?:rst|reset)', line, re.IGNORECASE):
            variants.append((line.replace('posedge', 'negedge', 1), 'reset_edge_flip'))

    # Reset value faults, and dropping the reset assignment entirely.
    if _in_reset_context(lines, index) and re.search(r'(?<![<>=!])<?=(?!=)', line):
        lhs, operator, rhs = _split_assignment(line)
        if lhs is not None:
            stripped_rhs = rhs.strip().rstrip(';').strip()
            for replacement, name in (("'0", 'reset_value_zero'), ("'1", 'reset_value_ones')):
                if stripped_rhs != replacement:
                    variants.append((_rebuild_assignment(lhs, operator, replacement), name))
            indent = re.match(r'\s*', line).group(0)
            variants.append((f'{indent}// removed by mutation: {line.strip()}', 'reset_removed'))

    return variants


def _rebuild_assignment(lhs: str, operator: str, rhs: str) -> str:
    return f'{lhs.rstrip()} {operator} {rhs.strip()};'


def _split_assignment(line: str) -> Tuple[Optional[str], str, str]:
    match = re.search(r'(?<![<>=!+\-*/%&|^])(<=|=)(?!=)', line)
    if not match:
        return None, '', ''
    return line[:match.start()], match.group(1), line[match.end():]


def _interface_mutations(line: str):
    variants = []

    # Mis-wire a named port connection by tying it low.  Clock and reset ports
    # are skipped: freezing them stops the whole design rather than injecting a
    # localised interface fault.  ALL-CAPS names are parameter overrides in a
    # ``#(...)`` block, not ports.
    port_connection = re.match(
        r'^(?P<head>\s*\.\s*(?P<port>\w+)\s*\()(?P<body>[^)]*)\)(?P<tail>.*)$', line
    )
    if port_connection and port_connection.group('body').strip():
        port = port_connection.group('port')
        is_parameter_override = port.isupper()
        if not _RESET_HINT_RE.search(port) and not _CLOCK_HINT_RE.search(port) \
                and not is_parameter_override:
            variants.append((
                f"{port_connection.group('head')}'0){port_connection.group('tail')}",
                'port_tied_low',
            ))

    # Tie an output assignment to a constant.
    lhs, operator, rhs = _split_assignment(line)
    if lhs is not None and re.search(r'\w+_o\b|\bo_\w+', lhs):
        stripped_rhs = rhs.strip().rstrip(';').strip()
        if stripped_rhs not in ("'0", "1'b0", '0'):
            variants.append((_rebuild_assignment(lhs, operator, "'0"), 'output_tied_low'))

    # Drop an output driver.
    if lhs is not None and re.search(r'\w+_o\b', lhs) and 'assign' in line:
        indent = re.match(r'\s*', line).group(0)
        variants.append((f'{indent}// removed by mutation: {line.strip()}', 'output_driver_removed'))

    return variants


def _state_update_mutations(line: str, state_signals: Sequence[str], state_labels: Sequence[str]):
    variants = []

    lhs, operator, rhs = _split_assignment(line)
    if lhs is None:
        return variants

    lhs_names = set(re.findall(r'[A-Za-z_]\w*', lhs))
    if not lhs_names & set(state_signals):
        return variants

    current = rhs.strip().rstrip(';').strip()

    # Hold the state instead of updating it.
    target_signal = next(iter(lhs_names & set(state_signals)))
    held = _held_counterpart(target_signal, state_signals)
    # A continuous assignment of a signal to itself is a combinational loop, so
    # the mutant would fail to elaborate instead of exposing a held-state fault.
    if held == target_signal and re.match(r'\s*assign\b', line):
        held = None
    if held and held != current:
        variants.append((_rebuild_assignment(lhs, operator, held), 'state_held'))

    # Redirect a transition to a different declared state.  Only signals that
    # already carry an enum label are retargeted: assigning a state label to a
    # plain register would be a type error rather than a functional fault.
    if current in state_labels:
        for label in state_labels:
            if label != current:
                variants.append((
                    _rebuild_assignment(lhs, operator, label),
                    f'state_target_{label}',
                ))
                break

    return variants


def _held_counterpart(signal: str, state_signals: Sequence[str]) -> Optional[str]:
    """Find the registered counterpart of a next-state signal.

    ``next_state``/``current_state`` and ``*_d``/``*_q`` are the two conventions
    used across the evaluated designs.
    """
    candidates = []
    if signal.endswith('_d'):
        candidates.append(signal[:-2] + '_q')
    if signal.startswith('next_'):
        candidates.append('current_' + signal[len('next_'):])
        candidates.append(signal[len('next_'):] + '_q')
    if signal.endswith('_q'):
        # A register assigned from itself never advances: a self-hold fault.
        candidates.append(signal)
    for candidate in candidates:
        if candidate in state_signals:
            return candidate
    # No registered counterpart exists in this module. Inventing one by naming
    # convention produces a mutant that fails to elaborate, which is wasted
    # formal time and would otherwise be silently excluded from the population.
    return None


def _discover_state_signals(rtl_text: str) -> Tuple[List[str], List[str]]:
    """Find state-holding signal names and enum state labels."""
    text = strip_sv_noise(rtl_text)
    signals = set(re.findall(r'\b(\w*state\w*|\w+_[dq])\b', text))
    labels = set()
    for match in re.finditer(r'\benum\b[^{]*\{(?P<body>[^}]*)\}', text, re.DOTALL):
        labels.update(re.findall(r'[A-Za-z_]\w*', match.group('body')))
    return sorted(signals), sorted(labels)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


@dataclass
class MutationConfig:
    """Bounds on the generated mutant population."""

    fault_classes: Tuple[FaultClass, ...] = tuple(FaultClass)
    #: Cap per fault class so no single class dominates the detection rate.
    max_per_class: int = 12
    max_total: int = 60
    #: Skip lines inside the module header (ports carry no behaviour to mutate).
    skip_header: bool = True
    #: Skip conditional generate branches that the elaboration parameters
    #: switch off; a fault there is invisible to any proof of this instance.
    skip_unelaborated: bool = True
    #: Parameter overrides applied at elaboration, on top of the module defaults.
    parameter_overrides: Dict[str, int] = field(default_factory=dict)


def generate_mutants(
    rtl_text: str,
    module_name: str = '',
    config: Optional[MutationConfig] = None,
) -> List[Mutant]:
    """Generate a unique, single-point mutant population for one module.

    Mutants are produced deterministically (stable ordering, stable ids) so a
    published detection rate can be reproduced exactly.
    """
    config = config or MutationConfig()
    lines = rtl_text.splitlines()
    header_end_line = _module_body_start_line(rtl_text) if config.skip_header else 0
    verification = verification_regions(lines)
    in_block_comment = block_comment_lines(lines)

    parameters = module_parameters(rtl_text)
    parameters.update(config.parameter_overrides)
    if config.skip_unelaborated:
        unelaborated = dead_generate_lines(lines, parameters)
        for index, is_dead in enumerate(ifdef_bug_dead_lines(lines)):
            unelaborated[index] = unelaborated[index] or is_dead
    else:
        unelaborated = [False] * len(lines)

    # State signals are read from the elaborated code only. A register declared
    # inside a generate branch the parameters switch off does not exist in this
    # instance, and holding a signal at it produces a mutant that fails to
    # elaborate rather than one that exposes a stuck-state fault.
    live_text = '\n'.join(
        line for index, line in enumerate(lines) if not unelaborated[index])
    state_signals, state_labels = _discover_state_signals(live_text)

    candidates_by_class: Dict[FaultClass, List[Tuple[int, str, str]]] = {
        fault: [] for fault in FaultClass
    }
    block_extents = sequential_block_extents(lines)
    in_sequential_block = False

    for index, line in enumerate(lines):
        if _ALWAYS_FF_RE.search(line):
            in_sequential_block = True
        elif re.search(r'\balways_comb\b|\balways\s*@\s*\*', line):
            in_sequential_block = False

        if index < header_end_line or verification[index] or unelaborated[index]:
            continue
        if in_block_comment[index]:
            continue

        # Operators see the code only; the trailing comment is re-attached so
        # the mutant keeps the original annotation next to the injected fault.
        code, comment = split_trailing_comment(line)
        if not _is_mutable_line(code):
            continue

        producers = (
            (FaultClass.CONTROL, _control_mutations(code)),
            (FaultClass.DATAPATH, _datapath_mutations(code)),
            (FaultClass.TIMING, _timing_mutations(
                code, in_sequential_block, lines, index, block_extents[index])),
            (FaultClass.RESET, _reset_mutations(code, lines, index)),
            (FaultClass.INTERFACE, _interface_mutations(code)),
            (FaultClass.STATE_UPDATE, _state_update_mutations(code, state_signals, state_labels)),
        )
        for fault_class, variants in producers:
            if fault_class not in config.fault_classes:
                continue
            for mutated_line, operator in variants:
                candidates_by_class[fault_class].append(
                    (index, operator, mutated_line + comment))

    # Select round-robin across fault classes rather than in file order, so a
    # class whose faults happen to sit late in the file is still represented.
    seen_fingerprints = {fingerprint_source(rtl_text)}
    per_class_count: Dict[FaultClass, int] = {fault: 0 for fault in FaultClass}
    mutants: List[Mutant] = []
    cursors: Dict[FaultClass, int] = {fault: 0 for fault in FaultClass}

    while len(mutants) < config.max_total:
        progressed = False
        for fault_class in config.fault_classes:
            if len(mutants) >= config.max_total:
                break
            if per_class_count[fault_class] >= config.max_per_class:
                continue

            queue = candidates_by_class[fault_class]
            while cursors[fault_class] < len(queue):
                index, operator, mutated_line = queue[cursors[fault_class]]
                cursors[fault_class] += 1

                if config.skip_unelaborated and elaboration_equivalent(
                        lines[index], mutated_line, parameters):
                    continue

                mutated_source = '\n'.join(
                    mutated_line if i == index else original
                    for i, original in enumerate(lines)
                )
                fingerprint = fingerprint_source(mutated_source)
                if fingerprint in seen_fingerprints:
                    continue  # duplicate edit, or a no-op after normalisation
                seen_fingerprints.add(fingerprint)

                per_class_count[fault_class] += 1
                mutants.append(Mutant(
                    mutant_id=(
                        f'{module_name or "mut"}_{fault_class.value}_'
                        f'{per_class_count[fault_class]:03d}'
                    ),
                    fault_class=fault_class,
                    operator=operator,
                    line_number=index + 1,
                    original_line=lines[index],
                    mutated_line=mutated_line,
                    source=mutated_source,
                    fingerprint=fingerprint,
                ))
                progressed = True
                break

        if not progressed:
            break

    mutants.sort(key=lambda m: (m.fault_class.value, m.line_number, m.operator))
    return mutants


def write_mutants(mutants: Sequence[Mutant], output_dir: str, extension: str = '.sv') -> Dict[str, str]:
    """Write each mutant to its own file and return ``{mutant_id: path}``."""
    os.makedirs(output_dir, exist_ok=True)
    paths = {}
    for mutant in mutants:
        path = os.path.join(output_dir, f'{mutant.mutant_id}{extension}')
        with open(path, 'w') as handle:
            handle.write(mutant.source)
        paths[mutant.mutant_id] = path
    return paths


# ---------------------------------------------------------------------------
# Non-equivalence screening
# ---------------------------------------------------------------------------

_MITER_TEMPLATE = '''// Auto-generated equivalence miter for mutation screening.
// Proving m_equiv_outputs means the mutant is logically equivalent to the
// reference and must be excluded from the mutant population.
module {miter_name} (
{port_decls}
);

{reference_module} u_ref (
{ref_connections}
);

{mutant_module} u_mut (
{mut_connections}
);

{output_decls}

a_equiv_outputs: assert property ({equivalence_expr})
else begin
    $display("Mutant differs from reference");
end

endmodule
'''


def build_equivalence_miter(
    rtl_text: str,
    module_name: str,
    mutant_module_name: str,
    miter_name: str = 'svapshot_miter',
) -> Optional[str]:
    """Generate a miter that asserts output equality of reference and mutant.

    Returns ``None`` when the module header cannot be parsed into a flat port
    list, in which case equivalence screening is skipped for that module.
    """
    from coi import _iter_ports, _PORT_DIRECTION_RE  # shared header parsing

    _, _, port_list, _ = split_module_header(strip_sv_noise(rtl_text), module_name)
    if not port_list.strip():
        return None
    # A Verilog-95 header names its ports without declaring them, so neither the
    # directions nor the widths this miter connects are in it.  Guessing would
    # tie a mutant's outputs to the reference's inputs and call every mutant
    # equivalent; skipping the screen only loses a screen.
    if not _PORT_DIRECTION_RE.search(port_list):
        return None

    inputs: List[str] = []
    outputs: List[str] = []
    port_types: Dict[str, str] = {}

    direction = 'input'
    for item in _split_ports_with_types(port_list):
        direction_match = re.search(r'\b(input|output|inout)\b', item)
        if direction_match:
            direction = direction_match.group(1)
        names = [name for _, name in _iter_ports(item)]
        if not names:
            continue
        name = names[-1]
        declaration = re.sub(r'\b(?:input|output|inout)\b', '', item).strip().rstrip(',')
        port_types[name] = declaration
        if direction == 'input':
            inputs.append(name)
        else:
            outputs.append(name)

    if not outputs:
        return None

    port_decls = ',\n'.join(f'    input {port_types[name]}' for name in inputs)
    output_decls = '\n'.join(
        f'{port_types[name].replace(name, f"ref_{name}")};\n'
        f'{port_types[name].replace(name, f"mut_{name}")};'
        for name in outputs
    )
    ref_connections = ',\n'.join(
        [f'    .{name}({name})' for name in inputs]
        + [f'    .{name}(ref_{name})' for name in outputs]
    )
    mut_connections = ',\n'.join(
        [f'    .{name}({name})' for name in inputs]
        + [f'    .{name}(mut_{name})' for name in outputs]
    )
    equivalence_expr = ' && '.join(f'(ref_{name} == mut_{name})' for name in outputs)

    return _MITER_TEMPLATE.format(
        miter_name=miter_name,
        port_decls=port_decls,
        reference_module=module_name,
        mutant_module=mutant_module_name,
        ref_connections=ref_connections,
        mut_connections=mut_connections,
        output_decls=output_decls,
        equivalence_expr=equivalence_expr,
    )


def _split_ports_with_types(port_list: str) -> List[str]:
    from coi import _split_top_level
    return [item for item in _split_top_level(port_list) if item.strip()]


def rename_module(rtl_text: str, old_name: str, new_name: str) -> str:
    """Rename a module declaration so reference and mutant can coexist."""
    return re.sub(
        rf'\bmodule\s+{re.escape(old_name)}\b',
        f'module {new_name}',
        rtl_text,
        count=1,
    )


def screen_non_equivalence(
    mutants: Sequence[Mutant],
    rtl_text: str,
    module_name: str,
    prove_equivalence: Callable[[str, str, str], EquivalenceVerdict],
) -> List[Mutant]:
    """Attach an equivalence verdict to every mutant.

    ``prove_equivalence`` receives ``(miter_source, mutant_source, mutant_id)``
    and returns the verdict for that mutant; the caller supplies it so this
    module stays independent of any particular formal tool driver.
    """
    for mutant in mutants:
        mutant_module = f'{module_name}_mut'
        mutant_source = rename_module(mutant.source, module_name, mutant_module)
        miter = build_equivalence_miter(rtl_text, module_name, mutant_module)
        if miter is None:
            mutant.equivalence = EquivalenceVerdict.UNKNOWN
            mutant.escape_reason = 'equivalence miter could not be generated'
            continue
        mutant.equivalence = prove_equivalence(miter, mutant_source, mutant.mutant_id)
    return mutants


def valid_population(mutants: Sequence[Mutant]) -> List[Mutant]:
    """Return only mutants formally established as non-equivalent.

    Equivalent, unknown, and unscreened edits are excluded. Treating
    ``not_screened`` as valid made syntactic rewrites and stale golden copies
    look like escaped bugs.
    """
    return [
        mutant for mutant in mutants
        if mutant.equivalence is EquivalenceVerdict.NON_EQUIVALENT
    ]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@dataclass
class MutationScore:
    """Mutation-detection results for one snapshot."""

    total_generated: int = 0
    excluded_equivalent: int = 0
    population: int = 0
    detected: int = 0
    survived: int = 0
    per_class: Dict[str, Dict[str, int]] = field(default_factory=dict)
    escape_reasons: Dict[str, int] = field(default_factory=dict)

    @property
    def detection_rate(self) -> float:
        return self.detected / self.population if self.population else 0.0

    def to_dict(self) -> dict:
        data = asdict(self)
        data['detection_rate'] = self.detection_rate
        return data


def score_mutants(mutants: Sequence[Mutant]) -> MutationScore:
    """Aggregate per-mutant outcomes into a detection rate with class breakdown."""
    score = MutationScore(total_generated=len(mutants))
    population = valid_population(mutants)
    score.excluded_equivalent = len(mutants) - len(population)
    score.population = len(population)

    for mutant in population:
        bucket = score.per_class.setdefault(
            mutant.fault_class.value, {'population': 0, 'detected': 0, 'survived': 0}
        )
        bucket['population'] += 1
        if mutant.detected:
            score.detected += 1
            bucket['detected'] += 1
        else:
            score.survived += 1
            bucket['survived'] += 1
            reason = mutant.escape_reason or 'no property covers the mutated logic'
            score.escape_reasons[reason] = score.escape_reasons.get(reason, 0) + 1

    return score


def format_mutation_report(score: MutationScore, mutants: Sequence[Mutant]) -> str:
    lines = [
        'Mutation-validated regression sensitivity',
        '=' * 60,
        f'  generated                {score.total_generated}',
        f'  excluded (equivalent)    {score.excluded_equivalent}',
        f'  valid population         {score.population}',
        f'  detected                 {score.detected}',
        f'  survived                 {score.survived}',
        f'  detection rate           {score.detection_rate:.1%}',
        '',
        'Per fault class:',
        f'  {"CLASS":<16}{"POP":>6}{"DETECTED":>10}{"RATE":>8}',
    ]
    for fault_class in FaultClass:
        bucket = score.per_class.get(fault_class.value)
        if not bucket:
            continue
        rate = bucket['detected'] / bucket['population'] if bucket['population'] else 0.0
        lines.append(
            f'  {fault_class.value:<16}{bucket["population"]:>6}'
            f'{bucket["detected"]:>10}{rate:>7.0%}'
        )

    survivors = [m for m in valid_population(mutants) if not m.detected]
    if survivors:
        lines.append('')
        lines.append('Surviving mutants (why the snapshot missed them):')
        for mutant in survivors:
            reason = mutant.escape_reason or 'no property covers the mutated logic'
            lines.append(f'  - {mutant.mutant_id}: {mutant.description()}')
            lines.append(f'      reason: {reason}')

    return '\n'.join(lines)


def dump_mutants(mutants: Sequence[Mutant], path: str) -> None:
    with open(path, 'w') as handle:
        json.dump([m.to_dict() for m in mutants], handle, indent=2)


def load_mutants(path: str, source_dir: str = '') -> List[Mutant]:
    """Load a frozen population and restore source bodies from separate files."""
    with open(path) as handle:
        values = json.load(handle)
    mutants = []
    for value in values:
        source = value.get('source', '')
        if source_dir and not source:
            candidates = (
                os.path.join(source_dir, value['mutant_id'] + '.sv'),
                os.path.join(source_dir, value['mutant_id'] + '.v'),
            )
            source_path = next((candidate for candidate in candidates
                                if os.path.isfile(candidate)), '')
            if source_path:
                with open(source_path, errors='replace') as handle:
                    source = handle.read()
        if not source:
            raise ValueError(
                f'frozen mutant {value.get("mutant_id")} has no source body')
        mutants.append(Mutant(
            mutant_id=value['mutant_id'],
            fault_class=FaultClass(value['fault_class']),
            operator=value['operator'],
            line_number=int(value['line_number']),
            original_line=value['original_line'],
            mutated_line=value['mutated_line'],
            source=source,
            fingerprint=value.get('fingerprint', ''),
            equivalence=EquivalenceVerdict(
                value.get('equivalence', EquivalenceVerdict.NOT_SCREENED.value)),
            detected_by=list(value.get('detected_by', [])),
            escape_reason=value.get('escape_reason', ''),
        ))
    return mutants
