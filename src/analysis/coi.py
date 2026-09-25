"""Cone-of-influence extraction and snapshot diversity metrics.

Checker coverage alone cannot say whether a snapshot inspects *different* parts
of a design: a set of near-identical properties can saturate a coverage number
while all looking at the same logic.  This module builds a structural fan-in
graph from the RTL and reports, per property, which design signals its cone of
influence touches.  From those cones it derives the diversity figures used as
primary snapshot-quality metrics:

* ``coi_coverage`` — fraction of design signals reachable from any property.
* ``mean_pairwise_jaccard_distance`` — how much the cones differ from each other.
* ``distinct_cone_signatures`` — number of genuinely different cones.
* ``redundancy`` — share of properties whose cone duplicates another's.

The analysis is deliberately tool-independent so the same numbers can be
reported for every baseline in the shared harness, whatever formal engine ran
the proofs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Dict, Iterable, List, Optional, Sequence, Set

#: Identifiers that appear in expressions but never denote design signals.
SV_RESERVED = frozenset({
    'always', 'always_comb', 'always_ff', 'always_latch', 'and', 'assert',
    'assign', 'assume', 'automatic', 'begin', 'bit', 'break', 'byte', 'case',
    'casex', 'casez', 'const', 'continue', 'cover', 'default', 'disable',
    'do', 'else', 'end', 'endcase', 'endfunction', 'endgenerate', 'endmodule',
    'endpackage', 'endtask', 'enum', 'extern', 'final', 'for', 'foreach',
    'forever', 'function', 'generate', 'genvar', 'if', 'iff', 'import',
    'initial', 'inout', 'input', 'int', 'integer', 'interface', 'localparam',
    'logic', 'longint', 'modport', 'module', 'nand', 'negedge', 'nor', 'not',
    'or', 'output', 'package', 'parameter', 'posedge', 'priority', 'property',
    'real', 'reg', 'return', 'shortint', 'signed', 'static', 'string',
    'struct', 'supply0', 'supply1', 'task', 'time', 'typedef', 'union',
    'unique', 'unique0', 'unsigned', 'var', 'void', 'wait', 'while', 'wire',
    'with', 'xor',
    # SVA operators and system functions
    'past', 'rose', 'fell', 'stable', 'changed', 'onehot', 'onehot0',
    'countones', 'isunknown', 'display', 'error', 'fatal', 'info', 'warning',
    'sampled', 'throughout', 'within', 'intersect', 'first_match',
    'clocking', 'endclocking', 'sequence', 'endsequence', 'endproperty',
    'implies', 'nexttime', 'until', 'until_with', 's_until', 's_eventually',
})

#: Words SystemVerilog reserved that Verilog leaves free.  A Verilog design may
#: use any of them as an identifier — ``generic_dpram``'s data output port is
#: called ``do`` — so parsing Verilog with the SystemVerilog keyword set deletes
#: real signals from the design.  The system-task names are here for the same
#: reason: a Verilog signal called ``stable`` or ``past`` is legal.
SV_ONLY_RESERVED = frozenset({
    'always_comb', 'always_ff', 'always_latch', 'assert', 'assume', 'bit',
    'break', 'byte', 'const', 'continue', 'cover', 'do', 'endpackage', 'enum',
    'final', 'foreach', 'iff', 'int', 'interface', 'logic', 'longint',
    'modport', 'package', 'priority', 'property', 'return', 'shortint',
    'static', 'string', 'struct', 'typedef', 'union', 'unique', 'unique0',
    'var', 'void',
    'past', 'rose', 'fell', 'stable', 'changed', 'onehot', 'onehot0',
    'countones', 'isunknown', 'sampled', 'throughout', 'within', 'intersect',
    'first_match', 'clocking', 'endclocking', 'sequence', 'endsequence',
    'endproperty', 'implies', 'nexttime', 'until', 'until_with', 's_until',
    's_eventually',
})

#: The Verilog-2001 subset of :data:`SV_RESERVED`.
VERILOG_RESERVED = SV_RESERVED - SV_ONLY_RESERVED


def reserved_words(language: str = 'sv') -> frozenset:
    """The keyword set to parse ``language`` with.

    ``'verilog'`` selects the Verilog-2001 keywords, which is what a ``.v``
    design is compiled as.  Assertion text is always SystemVerilog, so the
    default is the SystemVerilog set.
    """
    return VERILOG_RESERVED if language == 'verilog' else SV_RESERVED


def language_of(path) -> str:
    """``'verilog'`` for a Verilog source file, ``'sv'`` for anything else."""
    return 'verilog' if str(path).lower().endswith(('.v', '.vh')) else 'sv'


_IDENTIFIER_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\b')
_SIZED_LITERAL_RE = re.compile(r"\d*'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ?_]+")
_LINE_COMMENT_RE = re.compile(r'//[^\n]*')
_BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)
#: DOTALL matters: SystemVerilog string literals may be continued across lines
#: with a trailing backslash, and without it the closing quote of such a string
#: is mistaken for an opening quote and everything up to the next quote — real
#: code — is deleted.
_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"', re.DOTALL)


def strip_sv_noise(text: str) -> str:
    """Remove comments, strings, and sized literals before identifier scanning."""
    text = _BLOCK_COMMENT_RE.sub(' ', text)
    text = _LINE_COMMENT_RE.sub(' ', text)
    text = _STRING_RE.sub(' ', text)
    return _SIZED_LITERAL_RE.sub(' ', text)


def extract_identifiers(text: str, language: str = 'sv') -> Set[str]:
    """Extract candidate identifiers from a SystemVerilog fragment.

    Every component of a dotted reference is returned rather than just the root
    or just the leaf, because both forms carry meaning here: ``ptw_arb.state_q``
    is a module-qualified internal signal (the leaf is the signal) while
    ``ptw_dtlb_comm_o.resp.valid`` is a struct field access (the root is the
    signal).  Callers intersect the result with the set of known design signals,
    so components that denote neither are discarded there.
    """
    cleaned = strip_sv_noise(text)
    reserved = reserved_words(language)
    return {
        token for token in _IDENTIFIER_RE.findall(cleaned)
        if token.lower() not in reserved
    }


@dataclass
class SignalGraph:
    """Structural fan-in graph of one RTL module.

    ``fan_in[target]`` holds the identifiers that appear on the right-hand side
    of any assignment to ``target``, i.e. the signals that can influence it.
    """

    module: str = ''
    ports_in: Set[str] = field(default_factory=set)
    ports_out: Set[str] = field(default_factory=set)
    internals: Set[str] = field(default_factory=set)
    parameters: Set[str] = field(default_factory=set)
    fan_in: Dict[str, Set[str]] = field(default_factory=dict)

    @property
    def all_signals(self) -> Set[str]:
        return self.ports_in | self.ports_out | self.internals

    def cone_of_influence(self, seeds: Iterable[str], max_depth: int = 64) -> Set[str]:
        """Return the transitive fan-in closure of ``seeds``.

        Only identifiers known to the graph are returned, so cross-module and
        misspelled references are silently ignored rather than inflating the
        cone.
        """
        known = self.all_signals
        cone = {s for s in seeds if s in known}
        frontier = set(cone)

        for _ in range(max_depth):
            next_frontier: Set[str] = set()
            for signal in frontier:
                for source in self.fan_in.get(signal, ()):  # type: ignore[arg-type]
                    if source in known and source not in cone:
                        cone.add(source)
                        next_frontier.add(source)
            if not next_frontier:
                break
            frontier = next_frontier

        return cone


_PORT_DIRECTION_RE = re.compile(
    r'\b(?P<dir>input|output|inout)\b(?P<rest>[^;,)]*)',
)
_PARAM_RE = re.compile(
    r'\b(?:parameter|localparam)\b[^;=]*?(?P<name>[A-Za-z_]\w*)\s*=',
)
_ENUM_RE = re.compile(r'\benum\b[^{]*\{(?P<body>[^}]*)\}', re.DOTALL)
_CONT_ASSIGN_RE = re.compile(r'\bassign\b(?P<body>[^;]*);')

#: Net / variable type prefixes. An initialiser (``wire rst_n = ...``) is still
#: a declaration; ``do_add = a + b`` and ``next_q <= d`` are assignments.
_DECL_TYPE_KEYWORDS = frozenset({
    'automatic', 'bit', 'byte', 'const', 'int', 'integer', 'logic', 'longint',
    'real', 'reg', 'shortint', 'signed', 'static', 'supply0', 'supply1',
    'time', 'tri', 'tri0', 'tri1', 'triand', 'trior', 'trireg', 'unsigned',
    'var', 'wand', 'wire', 'wor',
})

#: Statement keywords that rule out a line being a signal declaration.
_NON_DECL_KEYWORDS = frozenset({
    'assign', 'always', 'always_ff', 'always_comb', 'always_latch', 'initial',
    'final', 'if', 'else', 'case', 'casex', 'casez', 'endcase', 'begin', 'end',
    'for', 'while', 'foreach', 'repeat', 'forever', 'return', 'break',
    'continue', 'module', 'endmodule', 'function', 'endfunction', 'task',
    'endtask', 'generate', 'endgenerate', 'import', 'export', 'package',
    'endpackage', 'typedef', 'interface', 'endinterface', 'modport', 'assert',
    'assume', 'cover', 'property', 'endproperty', 'sequence', 'endsequence',
    'bind', 'parameter', 'localparam', 'input', 'output', 'inout', 'genvar',
    'default', 'unique', 'priority', 'wait', 'disable', 'enum', 'struct',
    'union',
})
_PROCEDURAL_ASSIGN_RE = re.compile(
    r'(?P<lhs>[A-Za-z_][\w.\[\]:$\-+ ]*?)\s*(?P<op><=|=)(?!=)(?P<rhs>[^;]*);',
)
_ALWAYS_BLOCK_RE = re.compile(
    r'\balways(?:_ff|_comb|_latch)?\b(?P<header>\s*@\s*\([^)]*\))?',
)


def _names_from_decl_tail(tail: str, language: str = 'sv') -> List[str]:
    """Pull declared identifiers out of a declaration tail.

    Packed dimensions, unpacked dimensions and initialisers are removed first so
    that only the declared names remain.
    """
    tail = re.sub(r'\[[^\]]*\]', ' ', tail)
    tail = re.sub(r'=[^,]*', ' ', tail)
    reserved = reserved_words(language)
    names = []
    for token in re.findall(r'[A-Za-z_]\w*', tail):
        if token.lower() in reserved:
            continue
        names.append(token)
    return names


def _match_paren(text: str, open_index: int) -> int:
    """Return the index just past the ``)`` matching the ``(`` at ``open_index``."""
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == '(':
            depth += 1
        elif text[index] == ')':
            depth -= 1
            if depth == 0:
                return index + 1
    return len(text)


def _module_declaration(text: str, module_name: str = ''):
    """The ``module <name>`` match to split, or None when the text holds none.

    ``module_name`` picks one out of a file that declares several, which one
    module per file makes moot for SystemVerilog and classic Verilog does not:
    ``generic_dpram.v`` declares the RAM and two vendor variants of it, and
    reading the first one for all three describes the wrong design.  A name the
    text does not declare falls back to the first module, which is what a caller
    passing a label rather than a module name relies on.
    """
    declarations = list(re.finditer(r'\bmodule\s+(?P<name>\w+)', text))
    for match in declarations:
        if match.group('name') == module_name:
            return match
    return declarations[0] if declarations else None


def split_module_header(text: str, module_name: str = ''):
    """Locate the module header and split it from the body.

    Returns ``(module_name, param_list, port_list, body)``.  A balanced-paren
    scan is required because a module can carry ``import pkg::*;`` statements and
    a ``#(...)`` parameter block between the name and the port list, so the first
    semicolon after ``module <name>`` is not necessarily the end of the header.
    """
    match = _module_declaration(text, module_name)
    if not match:
        return '', '', '', text

    name = match.group('name')
    cursor = match.end()
    param_list = ''
    port_list = ''

    while cursor < len(text):
        char = text[cursor]
        if char.isspace():
            cursor += 1
        elif char == '#':
            open_index = text.find('(', cursor)
            if open_index == -1:
                break
            close_index = _match_paren(text, open_index)
            param_list = text[open_index + 1:close_index - 1]
            cursor = close_index
        elif char == '(':
            close_index = _match_paren(text, cursor)
            port_list = text[cursor + 1:close_index - 1]
            cursor = close_index
            break
        elif text.startswith('import', cursor) or text.startswith('export', cursor):
            semicolon = text.find(';', cursor)
            if semicolon == -1:
                break
            cursor = semicolon + 1
        else:
            # Anything else (e.g. an old-style header) means there is no
            # ANSI-style port list to extract.
            break

    semicolon = text.find(';', cursor)
    body = text[semicolon + 1:] if semicolon != -1 else text[cursor:]
    # Stop at this module's own end, or every module that follows in the file
    # contributes its declarations to this one's signal set.
    end = re.search(r'\bendmodule\b', body)
    if end:
        body = body[:end.start()]
    return name, param_list, port_list, body


def _split_top_level(text: str, separator: str = ',') -> List[str]:
    """Split on a separator that is not nested inside brackets."""
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    for char in text:
        if char in '([{':
            depth += 1
        elif char in ')]}':
            depth -= 1
        if char == separator and depth == 0:
            parts.append(''.join(current))
            current = []
        else:
            current.append(char)
    parts.append(''.join(current))
    return parts


def _iter_ports(port_list: str, language: str = 'sv'):
    """Yield ``(direction, port_name)`` for each port in an ANSI port list.

    A port that omits the direction keyword inherits it from the preceding port,
    and the declared name is the last identifier in the item once dimensions and
    default values have been stripped — which also discards the user-defined
    type name in declarations such as ``input tlb_ptw_comm_t itlb_ptw_comm_i``.
    """
    direction = 'input'
    for item in _split_top_level(port_list):
        direction_match = re.search(r'\b(input|output|inout)\b', item)
        if direction_match:
            direction = direction_match.group(1)
            item = item[direction_match.end():]
        names = _names_from_decl_tail(item, language)
        if names:
            yield direction, names[-1]


#: A port declared in the module body, which is where a Verilog-95 header's bare
#: name list leaves the directions.  Unlike the ANSI form the declaration runs to
#: the semicolon, so ``output full, full_r;`` declares two ports, not one.
_BODY_PORT_RE = re.compile(r'\b(?P<dir>input|output|inout)\b(?P<rest>[^;)]*);')
_SUBPROGRAM_RE = re.compile(
    r'\b(?:function|task)\b.*?\b(?:endfunction|endtask)\b', re.DOTALL)


def _body_port_directions(body: str, language: str = 'sv') -> Dict[str, str]:
    """The direction of each port declared in a module body.

    A subprogram declares the direction of its own arguments, which are locals
    and not ports, so those declarations are removed before the scan.
    """
    body = _SUBPROGRAM_RE.sub(' ', body)
    directions: Dict[str, str] = {}
    for match in _BODY_PORT_RE.finditer(body):
        for name in _names_from_decl_tail(match.group('rest'), language):
            directions[name] = match.group('dir')
    return directions


#: Characters that cannot appear in a bare declaration once packed and unpacked
#: dimensions have been removed.  Their presence marks the statement as an
#: expression, instantiation, or loop-header fragment instead.
_NON_DECL_CHARS = frozenset('(){}<>=!&|?:@+-*/%^~#."\'')


def is_declaration(statement: str) -> bool:
    """True when a statement looks like a signal declaration.

    Declarations are recognised by exclusion.  Splitting the module body on
    semicolons also yields for-loop header fragments (``i < LEVELS``) and module
    instantiations (``pseudoLRU #(...) u_plru (...)``), both of which would
    otherwise be mistaken for declarations and pollute the signal set.

    An initialiser (``wire rst_n = arst_n ^ ARST_LVL``) is still a declaration:
    the ``=`` is stripped before the exclusion check, matching
    ``_names_from_decl_tail``.
    """
    statement = statement.strip()
    if not statement:
        return False

    tokens = re.findall(r'[A-Za-z_]\w*', statement)
    if len(tokens) < 2 or tokens[0].lower() in _NON_DECL_KEYWORDS:
        return False

    without_dimensions = re.sub(r'\[[^\]]*\]', ' ', statement)
    without_initialiser = re.sub(r'=.*', ' ', without_dimensions)
    # ``name = expr`` / ``name <= expr`` without a type keyword is behaviour.
    # Stripping the initialiser would otherwise leave a bare identifier and
    # classify every function-body or combinational assign as a declaration.
    if re.search(r'(?<![<>=!])(?:<=|=)(?!=)', statement):
        if tokens[0].lower() not in _DECL_TYPE_KEYWORDS:
            return False
    return not any(char in _NON_DECL_CHARS for char in without_initialiser)


def build_signal_graph(rtl_text: str, module_name: str = '',
                       language: str = 'sv') -> SignalGraph:
    """Build a fan-in graph for one module of ``rtl_text``.

    The parse is intentionally lightweight: it recognises port directions,
    declarations, continuous assignments and procedural assignments.  Anything
    it cannot classify simply does not contribute edges, which makes cones
    conservative (never inflated by mis-parsing).

    ``language`` is ``'verilog'`` for a ``.v`` design, which changes which words
    are keywords rather than signals.
    """
    text = strip_sv_noise(rtl_text)
    parsed_name, param_list, port_list, body = split_module_header(text, module_name)
    graph = SignalGraph(module=module_name or parsed_name)
    reserved = reserved_words(language)

    # A Verilog-95 header lists bare port names and declares their directions in
    # the body.  Reading only the header there makes every port an input, which
    # is worse than not knowing: an environment assumption may constrain an
    # input, and an output that passes for one takes the property being proven
    # and turns it into a hypothesis.
    header_directions = _PORT_DIRECTION_RE.search(port_list)
    body_directions = ({} if header_directions
                       else _body_port_directions(body, language))

    for direction, port_name in _iter_ports(port_list, language):
        direction = body_directions.get(port_name, direction)
        if direction in ('input', 'inout'):
            graph.ports_in.add(port_name)
        if direction in ('output', 'inout'):
            graph.ports_out.add(port_name)

    for match in _PARAM_RE.finditer(param_list + ';' + body):
        graph.parameters.add(match.group('name'))

    # Enum labels behave as constants; recording them keeps them out of the
    # signal set while still letting callers recognise them as known names.
    for match in _ENUM_RE.finditer(body):
        for label in re.findall(r'[A-Za-z_]\w*', match.group('body')):
            if label.lower() not in reserved:
                graph.parameters.add(label)

    # Declarations inside a generate or always block are separated from the
    # surrounding code by begin/end rather than by a semicolon, and a `for`
    # header puts semicolons in the middle of a statement. Treating the block
    # keywords as statement separators, and dropping the `: label` a named
    # block leaves behind, is what makes a signal declared inside a generate
    # block visible at all — without it every cone stops at the generate
    # boundary and understates what the properties observe.
    # Compiler directives are not declarations, and `ifndef VERILATOR reads
    # exactly like one once the block keywords have become separators.
    declaration_text = re.sub(r'`\w+', ';', body)
    declaration_text = re.sub(
        r'\b(?:begin|end|generate|endgenerate|function|endfunction|task|endtask)\b',
        ';',
        declaration_text,
    )
    for statement in declaration_text.split(';'):
        statement = re.sub(r'^\s*:\s*\w+\b', ' ', statement)
        if not is_declaration(statement):
            continue
        # Drop the leading type tokens: the declared names are what remains
        # after the last type-like token sequence.
        tail = re.sub(r'^\s*(?:[A-Za-z_]\w*\s*(?:\[[^\]]*\]\s*)?)', ' ', statement, count=1)
        declared = _names_from_decl_tail(tail, language)
        for name in declared:
            if name in graph.ports_in or name in graph.ports_out:
                continue
            if name in graph.parameters:
                continue
            graph.internals.add(name)
        # `wire foo = expr` drives foo the same way a continuous assign does.
        if '=' in statement and declared:
            lhs, _, rhs = statement.partition('=')
            if rhs.strip():
                for name in declared:
                    graph.fan_in.setdefault(name, set()).update(
                        extract_identifiers(rhs, language) - {name})

    def add_edges(lhs_text: str, rhs_text: str) -> None:
        # A bit-select index on the left-hand side is a read, not a write, but
        # attributing it to the target keeps the cone conservative.
        targets = extract_identifiers(re.sub(r'\[[^\]]*\]', ' ', lhs_text), language)
        sources = extract_identifiers(rhs_text, language) | extract_identifiers(
            ' '.join(re.findall(r'\[([^\]]*)\]', lhs_text)), language
        )
        for target in targets:
            graph.fan_in.setdefault(target, set()).update(sources - {target})

    for match in _CONT_ASSIGN_RE.finditer(body):
        lhs, _, rhs = match.group('body').partition('=')
        if rhs:
            add_edges(lhs, rhs)

    # Procedural assignments: also treat the enclosing always-block sensitivity
    # list and any surrounding condition expressions as sources, since control
    # flow influences the assigned value.
    for block_text, block_conditions in _iter_procedural_blocks(body):
        for match in _PROCEDURAL_ASSIGN_RE.finditer(block_text):
            lhs = match.group('lhs')
            if re.search(r'\b(?:if|else|case|casez|casex|begin|end)\b', lhs):
                lhs = re.split(r'\b(?:if|else|case|casez|casex|begin|end)\b', lhs)[-1]
            add_edges(lhs, match.group('rhs') + ' ' + block_conditions)

    # Signals assigned nowhere still exist; make sure every declared name is a
    # graph node so cone lookups never miss them.
    for name in graph.all_signals:
        graph.fan_in.setdefault(name, set())

    return graph


def _iter_procedural_blocks(body: str):
    """Yield ``(block_text, condition_sources)`` for each always/initial block.

    ``condition_sources`` collects the sensitivity list plus every ``if``/``case``
    expression in the block, because those control which assignment executes.
    """
    matches = list(_ALWAYS_BLOCK_RE.finditer(body))
    if not matches:
        yield body, ''
        return

    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        block = body[start:end]

        conditions = [match.group('header') or '']
        conditions.extend(re.findall(r'\bif\s*\(([^;]*?)\)', block))
        conditions.extend(re.findall(r'\bcase[zx]?\s*\(([^)]*)\)', block))
        yield block, ' '.join(conditions)


@dataclass
class PropertyCone:
    """Cone of influence of a single property."""

    name: str
    referenced_signals: Set[str] = field(default_factory=set)
    cone: Set[str] = field(default_factory=set)

    @property
    def size(self) -> int:
        return len(self.cone)

    def signature(self) -> str:
        return ','.join(sorted(self.cone))

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'referenced_signals': sorted(self.referenced_signals),
            'cone_size': self.size,
            'cone': sorted(self.cone),
        }


@dataclass
class DiversityMetrics:
    """Snapshot-level cone diversity figures."""

    property_count: int = 0
    design_signal_count: int = 0
    union_cone_size: int = 0
    coi_coverage: float = 0.0
    mean_cone_size: float = 0.0
    distinct_cone_signatures: int = 0
    mean_pairwise_jaccard_distance: float = 0.0
    redundancy: float = 0.0
    uncovered_signals: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def property_expression(assertion_text: str) -> str:
    """Extract the property expression from an assertion block.

    Falls back to the whole block when the ``assert property`` header cannot be
    located, so malformed input still contributes its identifiers.
    """
    match = re.search(
        r'assert\s+property\s*\((?P<body>.*)\)\s*(?:else\b|;)',
        assertion_text,
        re.IGNORECASE | re.DOTALL,
    )
    if match:
        return match.group('body')
    return assertion_text


def assertion_name(assertion_text: str) -> Optional[str]:
    match = re.search(
        r'^\s*([A-Za-z0-9_]+)\s*:\s*(?:assert|assume|cover)\s',
        assertion_text,
        re.MULTILINE,
    )
    return match.group(1) if match else None


def compute_property_cones(
    assertions: Sequence[str],
    graph: SignalGraph,
) -> List[PropertyCone]:
    """Compute the cone of influence of every assertion against ``graph``."""
    cones: List[PropertyCone] = []
    for index, assertion in enumerate(assertions):
        name = assertion_name(assertion) or f'unnamed_{index}'
        referenced = extract_identifiers(property_expression(assertion))
        referenced &= graph.all_signals | graph.parameters
        cones.append(PropertyCone(
            name=name,
            referenced_signals=referenced,
            cone=graph.cone_of_influence(referenced),
        ))
    return cones


def _jaccard_distance(left: Set[str], right: Set[str]) -> float:
    union = left | right
    if not union:
        return 0.0
    return 1.0 - len(left & right) / len(union)


def compute_diversity(
    cones: Sequence[PropertyCone],
    graph: SignalGraph,
) -> DiversityMetrics:
    """Aggregate per-property cones into snapshot diversity metrics."""
    metrics = DiversityMetrics()
    design_signals = graph.all_signals
    metrics.property_count = len(cones)
    metrics.design_signal_count = len(design_signals)

    if not cones:
        metrics.uncovered_signals = sorted(design_signals)
        return metrics

    union_cone: Set[str] = set()
    for cone in cones:
        union_cone |= cone.cone

    metrics.union_cone_size = len(union_cone)
    if design_signals:
        metrics.coi_coverage = len(union_cone & design_signals) / len(design_signals)
    metrics.mean_cone_size = sum(c.size for c in cones) / len(cones)
    metrics.uncovered_signals = sorted(design_signals - union_cone)

    signatures = [c.signature() for c in cones]
    metrics.distinct_cone_signatures = len(set(signatures))
    metrics.redundancy = 1.0 - metrics.distinct_cone_signatures / len(cones)

    if len(cones) > 1:
        total = 0.0
        pairs = 0
        for i in range(len(cones)):
            for j in range(i + 1, len(cones)):
                total += _jaccard_distance(cones[i].cone, cones[j].cone)
                pairs += 1
        metrics.mean_pairwise_jaccard_distance = total / pairs

    return metrics


def analyse_snapshot_cones(
    assertions: Sequence[str],
    rtl_text: str,
    module_name: str = '',
) -> dict:
    """One-call cone analysis returning a JSON-serialisable report."""
    graph = build_signal_graph(rtl_text, module_name)
    cones = compute_property_cones(assertions, graph)
    metrics = compute_diversity(cones, graph)
    return {
        'module': graph.module,
        'metrics': metrics.to_dict(),
        'cones': {cone.name: cone.to_dict() for cone in cones},
    }


def format_diversity_summary(metrics: DiversityMetrics) -> str:
    return '\n'.join([
        'Cone-of-influence diversity:',
        f'  properties                     {metrics.property_count}',
        f'  design signals                 {metrics.design_signal_count}',
        f'  union cone size                {metrics.union_cone_size}',
        f'  COI coverage                   {metrics.coi_coverage:.1%}',
        f'  mean cone size                 {metrics.mean_cone_size:.1f}',
        f'  distinct cone signatures       {metrics.distinct_cone_signatures}',
        f'  mean pairwise Jaccard distance {metrics.mean_pairwise_jaccard_distance:.3f}',
        f'  cone redundancy                {metrics.redundancy:.1%}',
    ])
