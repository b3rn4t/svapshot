"""Whether a module is clocked, and by what.

A port called ``clk_i`` is a hint. The sensitivity list of a clocked block is
evidence. ``clock_divider_counter`` declares ``clk``, ``clk_div``,
``clk_div_valid`` and ``clk_out``, and no rule over those four names picks the
right one reliably; the module's own::

    always_ff @(posedge clk, negedge rstn)

says which signal the registers move on, that the reset is asynchronous, and
that it is active low. Reading the body first and the port names second is what
this module does.

Port names still matter for one large class of modules: the purely structural
ones, which instantiate clocked children without containing a clocked block of
their own. Forty-one of the modules under Sargantana's execution stage are of
that kind, so the body evidence extends the port heuristic rather than replacing
it.

Shared by ``main.py``, which classifies modules, and ``scaffold.engine``, which
writes the clocking block, so that the two cannot answer the question
differently.
"""

import collections
import re

CLOCK_NAME_HINTS = ('clk', 'clock')
RESET_NAME_HINTS = ('rst', 'reset')

#: Names that are unambiguous by convention; they outrank a lookalike.
EXACT_CLOCK_NAMES = ('clk', 'clk_i', 'clock', 'clock_i', 'clk_ci')
EXACT_RESET_NAMES = ('rst', 'rst_n', 'rstn', 'rst_i', 'rstn_i', 'rst_ni',
                     'reset', 'reset_n', 'resetn', 'reset_i', 'rst_rbi')

_BLOCK_COMMENT = re.compile(r'/\*.*?\*/', re.DOTALL)
_LINE_COMMENT = re.compile(r'//[^\n]*')
_STRING = re.compile(r'"(?:\\.|[^"\\])*"')

#: always_ff @(...) or plain always @(...); always_comb and always_latch have no
#: event control and so cannot name a clock.
_EVENT_CONTROL = re.compile(r'\balways(?:_ff)?\s*@\s*\(([^()]*)\)', re.IGNORECASE)
_EDGE = re.compile(r'\b(posedge|negedge)\s+([A-Za-z_]\w*)', re.IGNORECASE)

_ACTIVE_LOW_NAME = re.compile(
    r'(?:^|_)n(?:rst|reset)|(?:rst|reset)_?n(?:_|$)|_ni$|_n$|_l$')


Clocking = collections.namedtuple(
    'Clocking',
    'is_sequential clock clock_edge reset reset_active_low evidence')

#: Checker-local sampling clock for combinational DUTs. Concurrent assertions
#: need a clocking event; the DUT has none, so the property module owns this
#: net. Properties still write ``assert property ((expr))`` without ``@(...)``.
FORMAL_CLK = 'formal_clk'


def combinational_clock_hier(module_name):
    """Hierarchical path of the combinational checker's sampling clock."""
    return f'{module_name}.u_{module_name}_sva.{FORMAL_CLK}'


def ensure_combinational_clocking(prop_text):
    """Re-insert ``formal_clk`` + default clocking if a later rewrite dropped them.

    Concurrent ``assert property ((expr))`` has no clocking event inside the
    property. Jasper accepts that under ``clock -none``; VC Formal reports
    NCIFA unless the checker still has a default clocking block.
    """
    has_clock = bool(re.search(r'\b' + re.escape(FORMAL_CLK) + r'\b', prop_text))
    has_clocking = bool(re.search(r'\bdefault\s+clocking\b', prop_text))
    if has_clock and has_clocking:
        return prop_text
    lines = []
    if not has_clock:
        lines.append(f'logic {FORMAL_CLK};')
    if not has_clocking:
        lines.append(f'default clocking cb @(posedge {FORMAL_CLK});')
        lines.append('endclocking')
    block = '\n'.join(lines) + '\n'
    match = re.search(r'(genvar\s+j\s*;[ \t]*\n)', prop_text)
    if match:
        return prop_text[:match.end()] + block + prop_text[match.end():]
    marker = '//====DESIGNER-ADDED-SVA====//'
    index = prop_text.find(marker)
    if index != -1:
        return prop_text[:index] + block + '\n' + prop_text[index:]
    return block + prop_text

#: Where the answer came from, in decreasing order of confidence.
FROM_CLOCKED_BLOCK = 'clocked_block'
FROM_CLOCK_PORT = 'clock_port'
FROM_NOTHING = 'none'


def strip_noise(text):
    """Remove comments and string literals so they cannot match as code."""
    text = _BLOCK_COMMENT.sub(' ', text)
    text = _LINE_COMMENT.sub(' ', text)
    return _STRING.sub('""', text)


def looks_like_clock(name):
    """True for the port-name conventions used for clocks (clk_i, core_clock)."""
    lowered = name.lower()
    return (lowered.startswith(CLOCK_NAME_HINTS)
            or lowered.endswith(CLOCK_NAME_HINTS)
            or lowered.endswith(tuple(hint + '_i' for hint in CLOCK_NAME_HINTS)))


def looks_like_reset(name):
    """True for the port-name conventions used for resets (rstn_i, reset)."""
    lowered = name.lower()
    return (lowered.startswith(RESET_NAME_HINTS)
            or lowered.endswith(RESET_NAME_HINTS)
            or any(hint in lowered for hint in RESET_NAME_HINTS))


def reset_is_active_low_by_name(name):
    """Polarity guessed from the name, for resets no usage site explains."""
    return bool(_ACTIVE_LOW_NAME.search(name.lower()))


def clocked_blocks(rtl_text):
    """Every edge-sensitive block, as (edge, clock, [(reset, active_low)]).

    The first edge in a sensitivity list is the clock; anything after it is an
    asynchronous reset, and its edge gives the polarity directly. An extra edge
    whose name does not look like a reset is ignored rather than guessed at.
    """
    blocks = []
    for match in _EVENT_CONTROL.finditer(strip_noise(rtl_text)):
        edges = _EDGE.findall(match.group(1))
        if not edges:
            continue  # always @(a or b): combinational logic in old style
        clock_edge, clock = edges[0]
        resets = [(name, edge.lower() == 'negedge')
                  for edge, name in edges[1:] if looks_like_reset(name)]
        blocks.append((clock_edge.lower(), clock, resets))
    return blocks


def _vote(candidates):
    """Most frequent candidate, ties broken by first appearance."""
    if not candidates:
        return None
    counts = collections.Counter(candidates)
    best = max(counts.values())
    for candidate in candidates:
        if counts[candidate] == best:
            return candidate
    return None


def _preferred(candidates, exact_names):
    """A conventional name if one is present, else the first candidate."""
    for candidate in candidates:
        if candidate.lower() in exact_names:
            return candidate
    return candidates[0] if candidates else None


def _reset_polarity_from_usage(rtl_text, name):
    """Polarity of a synchronous reset, read from how the body tests it."""
    text = strip_noise(rtl_text)
    if re.search(r'\bif\s*\(\s*[!~]\s*' + re.escape(name) + r'\b', text):
        return True
    if re.search(r'\bif\s*\(\s*' + re.escape(name) + r'\b', text):
        return False
    return reset_is_active_low_by_name(name)


def _drivers_of(rtl_text, name):
    """Identifiers that continuously assign ``name``, if any."""
    pattern = re.compile(
        r'\b(?:assign\s+)?' + re.escape(name)
        + r'\s*=\s*([^;]+);')
    drivers = []
    for match in pattern.finditer(strip_noise(rtl_text)):
        drivers.extend(re.findall(r'[A-Za-z_]\w*', match.group(1)))
    return drivers


def analyze_clocking(rtl_text, ports=()):
    """Decide whether a module is clocked, and name its clock and reset.

    Args:
        rtl_text: the module source.
        ports: its input ports in declaration order. Only used when the body
            holds no clocked block, which is the case for modules that are
            purely structural. Callers that have no port list can omit it and
            keep whatever they derived themselves when ``clock`` comes back
            ``None``.

    Returns:
        Clocking. ``evidence`` says which source the answer came from, so a
        caller can treat a name read off a sensitivity list differently from one
        guessed at from a port list.
    """
    blocks = clocked_blocks(rtl_text)
    if blocks:
        clock = _vote([clock for _, clock, _ in blocks])
        clock_edge = _vote([edge for edge, name, _ in blocks if name == clock])
        resets = [reset for _, _, block_resets in blocks for reset in block_resets]
        reset_name = _vote([name for name, _ in resets])
        if reset_name is not None:
            active_low = _vote([low for name, low in resets if name == reset_name])
            # The sensitivity list may name an internal derived reset
            # (`wire rst_n = arst_n ^ ARST_LVL`). The checker and create_reset
            # need a primary input; when a port list is available, prefer one.
            port_set = set(ports)
            if port_set and reset_name not in port_set:
                drivers = set(_drivers_of(rtl_text, reset_name))
                reset_ports = [p for p in ports if looks_like_reset(p)]
                driven = [p for p in reset_ports if p in drivers]
                port_reset = _preferred(driven or reset_ports, EXACT_RESET_NAMES)
                if port_reset:
                    reset_name = port_reset
        else:
            # Synchronous reset: it is not in the sensitivity list, so look for
            # a reset-looking port and read its polarity off a usage site.
            reset_name = _preferred([p for p in ports if looks_like_reset(p)],
                                    EXACT_RESET_NAMES)
            active_low = (_reset_polarity_from_usage(rtl_text, reset_name)
                          if reset_name else False)
        return Clocking(True, clock, clock_edge, reset_name, bool(active_low),
                        FROM_CLOCKED_BLOCK)

    # No clocked block of its own. A clock port still means the module is
    # sequential: it either drives clocked children or registers through a
    # macro this parse does not follow.
    clock = _preferred([p for p in ports if looks_like_clock(p)], EXACT_CLOCK_NAMES)
    if clock:
        reset_name = _preferred([p for p in ports if looks_like_reset(p)],
                                EXACT_RESET_NAMES)
        active_low = (_reset_polarity_from_usage(rtl_text, reset_name)
                      if reset_name else False)
        return Clocking(True, clock, 'posedge', reset_name, bool(active_low),
                        FROM_CLOCK_PORT)

    return Clocking(False, None, None, None, False, FROM_NOTHING)


def module_type(rtl_text, ports=()):
    """'sequential' or 'combinational', for callers that only need the label."""
    return 'sequential' if analyze_clocking(rtl_text, ports).is_sequential \
        else 'combinational'
