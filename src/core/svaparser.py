import re
import sys
from typing import List, Tuple, Dict, Union, Any, Optional

# ── ANTLR-based structural extractor (imported lazily so that the module
#    remains usable even if the sva_grammar package is not on the path) ──
try:
    from sva_structural import extract_sva_structure as _antlr_extract
    _ANTLR_AVAILABLE = True
except Exception as _antlr_import_error:
    _ANTLR_AVAILABLE = False
    # Say so: the runtime is a declared dependency, and without it every
    # property takes the regex fallback without anything in the log to show it.
    print(f'WARNING: SVA grammar extractor unavailable '
          f'({_antlr_import_error}); falling back to regex preprocessing. '
          f'Install antlr4-python3-runtime from requirements.txt.',
          file=sys.stderr)

#: Distribution of OR over AND is exponential in the worst case.  A property
#: whose CNF exceeds this many clauses is reported as unprocessable rather than
#: expanded, because nothing downstream benefits from a clause list that large.
MAX_CNF_CLAUSES = 512


class SVAParser:
    #: Placeholders used while complex operands are held out of tokenisation.
    _PLACEHOLDER_RE = re.compile(r'__TERM_\d+__')

    def __init__(self):

        # Initial module testing attributes
        self.clauses = []  # List of clauses in CNF
        self.delay = 0
        self.name = ''

        # Dictionary to store [name, sva_string]
        self.sva_string = {}

        # Dictionary to store [name, delay] pairs
        self.delay_d = {}

        # Dictionary to store [name, clauses] pairs
        self.clauses_d = {}

        # Dictionary to store [name, postcondition] pairs, to avoid clause 'aliasing' when comparing
        self.postcondition_d = {}

        # Dictionary to store [name, result] pairs
        self.prop_result = {}

        # Set to track unprocessable properties
        self.unprocessable_properties = set()

        # Dictionary to store [name, sva_string] for unprocessable properties
        self.unprocessable_props_dict = {}

        # Dictionary to store [canonical assert property text, name] for
        # unprocessable properties, so the same property under a different
        # label is recognised instead of being counted as a new one.
        self.unprocessable_bodies = {}

        # Debug mode for verbose output
        self.debug_mode = False

        #self.current_name = ''

    def parse_sva(self, sva: str) -> Tuple[str, int, List[List[Union[str, bool]]], List[str]]:
        """
        Parse an SVA property into a list of propositional logic clauses.

        Structural extraction (label, disable iff, clock event, |-> / |=>,
        ##N delays) is handled by the ANTLR-backed SVAStructuralExtractor
        when available; the legacy regex pre-processor is used as a fallback.

        The CNF conversion pipeline (tokenize → convert_to_cnf → flatten_to_cnf)
        is the same regardless of which extractor ran.

        :param sva: Raw SVA assertion string.
        :return: (name, delay, cnf_clauses, postcondition_tokens)
        """
        name, delay, body, consequent_delay = self._extract_structure(sva)

        tokens = self.tokenize(body)
        clauses, postcondition = self.convert_to_cnf(tokens)

        if postcondition is not None:
            # Where the delay sits matters: 'a |-> ##2 b' and 'a ##2 b |-> c'
            # both sum to two, so the total alone would let two different
            # properties share one tuple and be taken for duplicates.
            postcondition = [f'##{consequent_delay}'] + list(postcondition)

        if self.debug_mode:
            print('TOKENS LIST: ' + str(tokens))
            print('POSTCONDITION: ' + str(postcondition))

        if clauses is not None:
            self.clauses.extend(clauses)
        return name, delay, clauses, postcondition

    # ------------------------------------------------------------------ #
    #  Structural extraction: ANTLR (primary) + legacy regex (fallback)   #
    # ------------------------------------------------------------------ #

    def _extract_structure(self, sva: str) -> Tuple[str, int, str, int]:
        """
        Dispatch to the ANTLR extractor; fall back to legacy regex preprocessing
        on any failure.  Returns (name, delay, body, consequent_delay) where
        *body* is ready for tokenize().
        """
        trimmed, synthetic = self._with_label(self._trim_to_property(sva))

        if _ANTLR_AVAILABLE:
            try:
                result = _antlr_extract(trimmed)
                if result.success:
                    if self.debug_mode:
                        print(f'[ANTLR] name={result.name}  delay={result.delay}  '
                              f'body={result.body}')
                    return ('' if synthetic else result.name, result.delay,
                            self._canonicalize_operators(result.body),
                            result.consequent_delay)
                if self.debug_mode:
                    print(f'[ANTLR] parse error — falling back. ({result.error})')
            except Exception as exc:
                if self.debug_mode:
                    print(f'[ANTLR] exception — falling back. ({exc})')

        name, delay, body, consequent_delay = self._legacy_preprocess(trimmed)
        if synthetic:
            name = ''
        return name, delay, self._canonicalize_operators(body), consequent_delay

    def _legacy_preprocess(self, sva: str) -> Tuple[str, int, str, int]:
        """
        Original regex-based structural pre-processor (kept as fallback).
        Returns (name, delay, body, consequent_delay).
        """
        # ── Operator normalisation ────────────────────────────────────
        # Only the outermost implication is structural, and only its delay is
        # recorded.  A nested one is left for the operator canonicaliser, which
        # rewrites the overlapping form and keeps the delayed form so that the
        # conversion refuses a delay the tuple cannot represent.
        implication = re.search(r'\|->|\|=>', sva)
        if implication:
            replacement = '->' if implication.group(0) == '|->' else '->##1'
            sva = (sva[:implication.start()] + replacement
                   + sva[implication.end():])
        sva = sva.replace(" or ",  "||")
        sva = sva.replace(" and ", "&&")

        # ── Clock event removal (@(…)) ────────────────────────────────
        sva = re.sub(r"@\s*\([^)]*\)\s*", "", sva)

        # ── Disable iff removal (whitespace-collapsed path) ───────────
        sva_nowhitespace = re.sub(r"\s+", "", sva)

        if "disableiff" in sva_nowhitespace:
            disableiff_positions = []
            start_pos = 0
            while True:
                pos = sva_nowhitespace.find("disableiff", start_pos)
                if pos == -1:
                    break
                disableiff_positions.append(pos)
                start_pos = pos + 10

            for pos in reversed(disableiff_positions):
                orig_pos = 0
                no_ws_idx = 0
                while no_ws_idx < pos and orig_pos < len(sva):
                    if not sva[orig_pos].isspace():
                        no_ws_idx += 1
                    orig_pos += 1

                paren_start = sva_nowhitespace.find("(", pos)
                if paren_start != -1:
                    count = 1
                    i = paren_start + 1
                    while i < len(sva_nowhitespace) and count > 0:
                        if sva_nowhitespace[i] == "(":
                            count += 1
                        elif sva_nowhitespace[i] == ")":
                            count -= 1
                        i += 1

                    if count == 0:
                        paren_end_no_ws = i
                        orig_end_pos = 0
                        no_ws_idx = 0
                        while no_ws_idx < paren_end_no_ws and orig_end_pos < len(sva):
                            if not sva[orig_end_pos].isspace():
                                no_ws_idx += 1
                            orig_end_pos += 1

                        disable_keyword_pos = sva.find(
                            "disable", max(0, orig_pos - 10), orig_pos + 10
                        )
                        if disable_keyword_pos == -1:
                            disable_keyword_pos = sva.find(
                                "disableiff", max(0, orig_pos - 10), orig_pos + 10
                            )

                        if disable_keyword_pos != -1:
                            sva = sva[:disable_keyword_pos] + sva[orig_end_pos:]

        sva = re.sub(r"disable\s*iff\s*\([^)]*\)\s*", "", sva)
        sva = re.sub(r"disableiff\s*\([^)]*\)\s*", "", sva)

        def _remove_balanced(s, pattern):
            while re.search(pattern, s):
                m = re.search(pattern, s)
                if not m:
                    break
                start = m.start()
                open_paren = s.find("(", start)
                if open_paren == -1:
                    break
                count, i = 1, open_paren + 1
                while i < len(s) and count > 0:
                    if s[i] == "(":
                        count += 1
                    elif s[i] == ")":
                        count -= 1
                    i += 1
                if count == 0:
                    s = s[:start] + s[i:]
            return s

        sva = _remove_balanced(sva, r"disable\s*iff\s*\(")
        sva = _remove_balanced(sva, r"disableiff\s*\(")
        sva = _remove_balanced(sva, r"@\s*\(")

        sva = re.sub(r"disable\s*iff\s*\([^\)]*\)", "", sva)
        sva = re.sub(r"disableiff\s*\([^\)]*\)", "", sva)

        if self.debug_mode:
            if "disable" in sva or "disableiff" in sva:
                print(f"WARNING: disable clause may still be present in: {sva}")
            print(f'[legacy] After removing @/disable clauses: {sva}')

        # ── Name and body extraction ──────────────────────────────────
        # Neither part may be indexed blindly: an unlabelled property and one
        # without its terminating semicolon are both legal enough to reach here,
        # and raising out of the parser turned them into 'parser exception',
        # which costs the assertion an LLM repair attempt and then a quarantine.
        sva = re.sub(r"\s+", "", sva)
        name_match = re.search(r"([a-zA-Z0-9_]+):assertproperty", sva)
        name = name_match.group(1) if name_match else ''

        head = sva.find("assertproperty(")
        end = (self._balanced_end(sva, head + len("assertproperty"))
               if head != -1 else -1)
        if end != -1 and not self._property_tail_is_benign(sva[end + 1:]):
            end = -1
        # An absent, unbalanced or truncated body yields no tokens, which the CNF
        # pipeline refuses; that is the honest outcome rather than an exception.
        body = sva[head + len("assertproperty("):end] if end != -1 else ''

        # ── Delay extraction ──────────────────────────────────────────
        delays = [int(x) for x in re.findall(r"##([0-9]+)", body)]
        delay = sum(delays)
        # '|=>' was rewritten to '->##1' above, so the consequent's share of the
        # delay is whatever sits to the right of the implication.
        implication = body.find("->")
        consequent = body[implication + 2:] if implication != -1 else body
        consequent_delay = sum(
            int(x) for x in re.findall(r"##([0-9]+)", consequent))
        if delays:
            # Only leading delays are dropped, since those are reported through
            # *consequent_delay*.  An interior delay separates two operands of a
            # sequence and has to stay in the text, both to keep them apart and
            # to agree with what the ANTLR extractor produces.
            previous = None
            while previous != body:
                previous = body
                body = re.sub(r"^##[0-9]+", "", body)
                body = re.sub(r"(?<=->)##[0-9]+", "", body)

        return name, delay, body, consequent_delay

    #: Left operand of a comparison: a name, possibly hierarchical and indexed.
    _OPERAND_RE = r"[A-Za-z_$][A-Za-z0-9_$.]*(?:\[[^\[\]]*\])*"

    #: A comparison whose right operand is a group or a concatenation.
    _COMPARISON_RE = re.compile(
        _OPERAND_RE + r"(?:[=!]==?|[<>]=?)[!~]*(?=[({])")

    #: A comparison operator, with any negation applied to its right operand.
    _COMPARISON_OP_RE = re.compile(r"(?:[=!]==?|[<>]=?)[!~]*")

    #: Right operand of a comparison when it is not a group.
    _RIGHT_OPERAND_RE = re.compile(
        r"[A-Za-z0-9_$'][A-Za-z0-9_$'.]*(?:\[[^\[\]]*\])*")

    @staticmethod
    def _balanced_end(text: str, open_index: int) -> int:
        """Index of the bracket closing the one at *open_index*, or -1."""
        closers = {'(': ')', '[': ']', '{': '}'}
        opener = text[open_index] if open_index < len(text) else ''
        closer = closers.get(opener)
        if closer is None:
            return -1
        depth = 0
        for j in range(open_index, len(text)):
            if text[j] == opener:
                depth += 1
            elif text[j] == closer:
                depth -= 1
                if depth == 0:
                    return j
        return -1

    @classmethod
    def _canonical_atom(cls, text: str) -> str:
        """Drop parentheses that enclose a whole operand.

        '(state_q == IDLE) && valid_i' and 'state_q == IDLE && valid_i' are the
        same property, and the database compares operand text, so the two
        spellings have to reduce to one.
        """
        while text.startswith('(') and cls._balanced_end(text, 0) == len(text) - 1:
            inner = text[1:-1].strip()
            if not inner:
                break
            text = inner
        return text

    @staticmethod
    def _is_balanced(text: str) -> bool:
        """True when every bracket in *text* is closed in order."""
        stack = []
        pairs = {')': '(', ']': '[', '}': '{'}
        for char in text:
            if char in '([{':
                stack.append(char)
            elif char in pairs:
                if not stack or stack.pop() != pairs[char]:
                    return False
        return not stack

    @classmethod
    def _comparison_atoms(cls, expression: str) -> List[str]:
        """Comparisons against a grouped right operand, taken whole.

        'in_ready_o == (in_valid_i && fmt_ready[fmt_i])' is one predicate: the
        '&&' belongs to the compared value, not to the property.  Reading it as
        structure split the comparison across two operands.
        """
        atoms = []
        for match in cls._COMPARISON_RE.finditer(expression):
            closing = cls._balanced_end(expression, match.end())
            if closing != -1:
                atoms.append(expression[match.start():closing + 1])

        # The compared value can also sit on the left, as in
        # '(remainder >= divisor) == quotient_bit'.  The group is then part of
        # the comparison rather than a grouping of the property.
        for index, char in enumerate(expression):
            if char != '(':
                continue
            closing = cls._balanced_end(expression, index)
            if closing == -1:
                continue
            operator = cls._COMPARISON_OP_RE.match(expression, closing + 1)
            if operator is None:
                continue
            end = cls._operand_end(expression, operator.end())
            if end is not None:
                atoms.append(expression[index:end])
        return atoms

    @classmethod
    def _operand_end(cls, expression: str, index: int) -> Optional[int]:
        """End of the operand starting at *index*, or None."""
        if index >= len(expression):
            return None
        if expression[index] in '([{':
            closing = cls._balanced_end(expression, index)
            return None if closing == -1 else closing + 1
        match = cls._RIGHT_OPERAND_RE.match(expression, index)
        if match is None:
            return None
        # A parenthesis straight after the name is an argument list, so the
        # operand is the whole call: stopping at '$past' truncates it and leaves
        # the comparison operator without a right side.
        if match.end() < len(expression) and expression[match.end()] == '(':
            closing = cls._balanced_end(expression, match.end())
            return None if closing == -1 else closing + 1
        return match.end()

    #: A comparison whose right operand is negated, as in 'x == !$past(y)'.
    _NEGATED_COMPARISON_RE = re.compile(
        _OPERAND_RE + r'(?:[=!]==?|[<>]=?)[!~]+')

    @classmethod
    def _negated_comparison_atoms(cls, expression: str) -> List[str]:
        """Comparisons against a negated operand, taken whole.

        'ready_o == !$past(pending_q)' is one predicate. Reading the '!' as a
        connective leaves 'ready_o ==' as an operand of its own, and the
        property is then refused although it is perfectly representable.
        """
        atoms = []
        for match in cls._NEGATED_COMPARISON_RE.finditer(expression):
            end = cls._operand_end(expression, match.end())
            if end is not None:
                atoms.append(expression[match.start():end])
        return atoms

    #: A reduction operator applied to a group: '|(a & b)', '~&(a | b)'.
    _REDUCTION_RE = re.compile(r'(?:~[&|^]|[&|^])\s*\(')

    @classmethod
    def _reduction_atoms(cls, expression: str) -> List[str]:
        """A reduction over a group, taken whole.

        '|(en_i & mask_i)' is a single boolean: the operator reduces a vector
        rather than joining two operands, which is what distinguishes it from
        the connective of the same spelling — nothing precedes it.
        """
        atoms = []
        for match in cls._REDUCTION_RE.finditer(expression):
            before = expression[:match.start()]
            previous = before[-1] if before else ''
            # The second half of '&&(' or '||(' is not an operator of its own,
            # and an operand before the operator makes it a binary one.
            if previous == expression[match.start()]:
                continue
            stripped = before.rstrip()
            last = stripped[-1] if stripped else ''
            if last and (last.isalnum() or last in "_$')]}"):
                continue
            closing = cls._balanced_end(expression, match.end() - 1)
            if closing != -1:
                atoms.append(expression[match.start():closing + 1])
        return atoms

    @classmethod
    def _call_atoms(cls, expression: str) -> List[str]:
        """Call operands such as $rose(a) or pkg::f(x), as single atoms.

        A parenthesis directly after an identifier is an argument list, not a
        grouping: splitting '$rose(req_i)' leaves two operands where the
        property has one, and the extra operand is then dropped silently.
        """
        atoms = []
        # Scope qualification is '::'.  Accepting a single colon here read the
        # else arm of a conditional, 'a : (b ? c : d)', as a call named 'a:'.
        for match in re.finditer(
                r'\$?[A-Za-z_][A-Za-z0-9_.]*(?:::[A-Za-z_][A-Za-z0-9_.]*)*(?=\()',
                expression):
            end = cls._balanced_end(expression, match.end())
            if end != -1:
                atoms.append(expression[match.start():end + 1])
        return atoms

    #: The head of an assertion. The label is optional because SVA makes it so.
    _ASSERT_HEAD_RE = re.compile(
        r'(?:([A-Za-z_]\w*)\s*:\s*)?assert\s*property\s*\(')

    #: Stands in for a missing label, both for the grammar and in the database.
    _SYNTHETIC_LABEL = 'unnamed_property'

    @staticmethod
    def _property_tail_is_benign(tail: str) -> bool:
        """True when only a terminator, a comment or a failure action follows.

        Anything else means the parenthesis that closed first is not the end of
        the property, so reading the body up to it would quietly drop the rest.
        'else' is matched without a word boundary because the text arrives with
        its whitespace removed, as 'elsebegin$display(...);end'.
        """
        tail = tail.strip()
        return not tail or bool(re.match(r';?\s*(else|//|/\*|$)', tail))

    @classmethod
    def _trim_to_property(cls, sva: str) -> str:
        """Reduce an assertion to 'label: assert property ( body );'.

        Generated assertions carry a failure action — 'else begin $display(...);
        end' — and usually wrap the body in a redundant pair of parentheses.
        The action block defeats the grammar, and the extra parentheses hide the
        implication inside a group, which turned the whole property into a
        single opaque operand.
        """
        match = cls._ASSERT_HEAD_RE.search(sva)
        if not match:
            return sva
        end = cls._balanced_end(sva, match.end() - 1)
        if end == -1:
            return sva

        if not cls._property_tail_is_benign(sva[end + 1:]):
            return sva

        body = sva[match.end():end].strip()
        prefix = []
        while True:
            progressed = False

            while body.startswith('(') \
                    and cls._balanced_end(body, 0) == len(body) - 1:
                inner = body[1:-1].strip()
                if not inner:
                    break
                body, progressed = inner, True

            # A clock event or reset guard nested inside the body keeps the
            # implication inside a group, where it stops being structural.
            # Hoisting the guard out also restores the space in 'disable iff':
            # the agent hands over whitespace-free text, in which 'disableiff'
            # reads as an ordinary identifier.
            clock = re.match(r'@\s*\(', body)
            if clock:
                closing = cls._balanced_end(body, clock.end() - 1)
                if closing != -1:
                    prefix.append(body[:closing + 1])
                    body = body[closing + 1:].strip()
                    continue

            guard = re.match(r'disable\s*iff\s*\(', body)
            if guard:
                closing = cls._balanced_end(body, guard.end() - 1)
                if closing != -1:
                    prefix.append(f'disable iff ({body[guard.end():closing]})')
                    body = body[closing + 1:].strip()
                    continue

            if not progressed:
                break

        head = (' '.join(prefix) + ' ') if prefix else ''
        label = f'{match.group(1)}: ' if match.group(1) else ''
        return f'{label}assert property ({head}{body});'

    @classmethod
    def _with_label(cls, sva: str) -> Tuple[str, bool]:
        """Give an unlabelled property a placeholder label.

        'assert property (...)' without a label is legal SVA, and the grammar
        requires one, so an unlabelled property used to reach the regex fallback
        and raise there.  It is parsed under a placeholder instead and the empty
        name is reported back, keeping the property itself intact.

        Returns (text, label_was_added).
        """
        match = cls._ASSERT_HEAD_RE.search(sva)
        if match is None or match.group(1):
            return sva, False
        return (f'{sva[:match.start()]}{cls._SYNTHETIC_LABEL}: '
                f'{sva[match.start():]}', True)

    @classmethod
    def _copy_reduction_operand(cls, body: str, index: int,
                                result: List[str]) -> int:
        """Copy the group a reduction applies to, unchanged.

        In '|(en_i & mask_i)' that '&' is a bitwise AND of two vectors, not a
        connective: rewriting it as '&&' changes what the operand means, and two
        different properties would then share one operand text.
        """
        if index < len(body) and body[index] == '(':
            closing = cls._balanced_end(body, index)
            if closing != -1:
                result.append(body[index:closing + 1])
                return closing + 1
        return index

    @classmethod
    def _canonicalize_operators(cls, body: str) -> str:
        """Rewrite single-bit bitwise connectives in their logical form.

        Generated assertions mix '&' with '&&' and '~' with '!' on one-bit
        control signals.  Read literally, 'a & b & c |-> d' has no boolean
        structure at all, so it collapses into one operand and no clause is
        unrolled.  Reduction operators keep their meaning: a '&' is a connective
        only where an operand precedes it.
        """
        result: List[str] = []
        i = 0
        while i < len(body):
            if body[i:i + 3] in ('|->', '|=>'):
                # The structural implication has already been rewritten, so one
                # appearing here is nested inside the property.  Overlapping, it
                # is an implication between values of the same cycle.  Delayed,
                # it carries a second delay, and the tuple records one: it is
                # left as it stands, which makes the conversion refuse it.
                result.append('->' if body[i:i + 3] == '|->' else body[i:i + 3])
                i += 3
                continue
            pair = body[i:i + 2]
            if pair in ('&&', '||'):
                result.append(pair)
                i += 2
                continue
            if pair in ('~&', '~|', '~^', '^~'):
                result.append(pair)
                i = cls._copy_reduction_operand(body, i + 2, result)
                continue

            char = body[i]
            previous = result[-1][-1] if result else ''
            if char in '&|^':
                binary = bool(previous) and (previous.isalnum()
                                             or previous in "_$')]}")
                if not binary:
                    result.append(char)
                    i = cls._copy_reduction_operand(body, i + 1, result)
                    continue
                result.append(char * 2 if char in '&|' else char)
            elif char == '~':
                following = body[i + 1] if i + 1 < len(body) else ''
                result.append('!' if (following.isalnum()
                                      or following in '_$(!~') else '~')
            else:
                result.append(char)
            i += 1
        return ''.join(result)

    @classmethod
    def canonical_property_text(cls, sva: str) -> str:
        """The 'assert property (...)' part alone, whitespace removed.

        Used to compare properties the CNF pipeline cannot represent. The label
        and the failure action are excluded so that the same unparsable property
        under a new name is recognised instead of counting as a new one.
        """
        text = re.sub(r'\s+', '', sva)
        index = text.find('assertproperty(')
        if index == -1:
            return text
        end = cls._balanced_end(text, index + len('assertproperty'))
        return text[index:] if end == -1 else text[index:end + 1]

    @staticmethod
    def _paren_equality_atoms(expression: str) -> List[str]:
        """Innermost (...) groups containing ==/!= but no top-level &&/||."""
        atoms = []
        i = 0
        while i < len(expression):
            if expression[i] != '(':
                i += 1
                continue
            depth = 0
            for j in range(i, len(expression)):
                if expression[j] == '(':
                    depth += 1
                elif expression[j] == ')':
                    depth -= 1
                    if depth == 0:
                        candidate = expression[i:j + 1]
                        inner = candidate[1:-1]
                        if (
                            re.search(r'[=!]=+', inner)
                            and '&&' not in inner
                            and '||' not in inner
                        ):
                            atoms.append(candidate)
                        break
            i += 1
        return atoms

    def tokenize(self, expression: str) -> List[str]:
        """
        Tokenize the SVA expression into manageable parts.
        Focus on boolean operators: &&, ||, ! and preserve complex expressions as atomic terms.

        :param expression: SVA expression.
        :return: List of tokens.
        """

        # Step 1: Identify and preserve complex expressions that should remain as single tokens
        # These include equality/inequality expressions with negation or complex operands

        preserved_expressions = []

        # Pattern to match expressions like: signal==!other_signal, signal!=!other_signal
        # and other complex equality patterns including those with ~ operator
        eq_patterns = [
            r'[a-zA-Z_][a-zA-Z0-9_\[\]:\.\*]*\s*==\s*!\s*[a-zA-Z_][a-zA-Z0-9_\[\]:\.\*]*',  # var == !var
            r'[a-zA-Z_][a-zA-Z0-9_\[\]:\.\*]*\s*!=\s*!\s*[a-zA-Z_][a-zA-Z0-9_\[\]:\.\*]*',  # var != !var
            r'[a-zA-Z_][a-zA-Z0-9_\[\]:\.\*]*\s*==\s*~\s*\([^)]*\)',  # var == ~(expr)
            r'[a-zA-Z_][a-zA-Z0-9_\[\]:\.\*]*\s*!=\s*~\s*\([^)]*\)',  # var != ~(expr)
            r'[a-zA-Z_][a-zA-Z0-9_\[\]:\.\*]*\s*==\s*\([^)]*\)',  # var == (expr)
            r'[a-zA-Z_][a-zA-Z0-9_\[\]:\.\*]*\s*!=\s*\([^)]*\)',  # var != (expr)
            r'[a-zA-Z_][a-zA-Z0-9_\[\]:\.\*]*\s*==\s*\{[^}]*\}',  # var == {concatenation}
            r'[a-zA-Z_][a-zA-Z0-9_\[\]:\.\*]*\s*!=\s*\{[^}]*\}',  # var != {concatenation}
            r'[a-zA-Z_][a-zA-Z0-9_\[\]:\.\*]*\s*==\s*\$\w+\([^)]*\)',  # var == $function(args)
            r'[a-zA-Z_][a-zA-Z0-9_\[\]:\.\*]*\s*!=\s*\$\w+\([^)]*\)',  # var != $function(args)
            r'[a-zA-Z_][a-zA-Z0-9_\[\]:\.\*]*\s*==\s*[a-zA-Z_][a-zA-Z0-9_\.\*]*\[[^\]]*[\+\-\*\/\%\<\>][^\]]*\]',  # var == array[complex_index]
            r'[a-zA-Z_][a-zA-Z0-9_\[\]:\.\*]*\s*!=\s*[a-zA-Z_][a-zA-Z0-9_\.\*]*\[[^\]]*[\+\-\*\/\%\<\>][^\]]*\]',  # var != array[complex_index]
            r'[a-zA-Z_][a-zA-Z0-9_\[\]:\.\*]*\s*==\s*[0-9]+\'[bhdBHD][0-9a-fA-F]+',  # var == 32'd0, 1'b0, etc.
            r'[a-zA-Z_][a-zA-Z0-9_\[\]:\.\*]*\s*!=\s*[0-9]+\'[bhdBHD][0-9a-fA-F]+',  # var != 32'd0, 1'b0, etc.
        ]

        # Operands: a comparison or a call is one term however much boolean
        # text its right side or its arguments contain.
        for pattern in eq_patterns:
            matches = re.findall(pattern, expression)
            preserved_expressions.extend(matches)

        preserved_expressions.extend(self._comparison_atoms(expression))
        preserved_expressions.extend(self._negated_comparison_atoms(expression))
        preserved_expressions.extend(self._reduction_atoms(expression))
        preserved_expressions.extend(self._call_atoms(expression))

        # Bare groups: these are structure, not operands.  Preserving one that
        # contains top-level && or || collapses a whole antecedent into a single
        # token and no clause is unrolled from it.
        groups = self._paren_equality_atoms(expression)
        groups += re.findall(r'\([^)]*\s+inside\s+\{[^}]*\}[^)]*\)', expression)
        preserved_expressions.extend(
            group for group in groups
            if '&&' not in group and '||' not in group)

        # Stable de-duplication (longest-first replacement still works).  A
        # candidate with unmatched brackets is dropped: substituting it would
        # leave the remaining text unbalanced.
        preserved_expressions = [
            candidate for candidate in dict.fromkeys(preserved_expressions)
            if self._is_balanced(candidate)]
        replacements = {}
        working_expr = expression

        # Sort by length (descending) to replace longer expressions first
        preserved_expressions.sort(key=len, reverse=True)

        for i, expr in enumerate(preserved_expressions):
            placeholder = f"__TERM_{i}__"
            replacements[placeholder] = expr
            working_expr = working_expr.replace(expr, placeholder)

        # Step 3: Simple tokenization focusing on boolean operators
        tokens = []
        i = 0
        current_token = ""
        conditional = self._ternary_present(working_expr)
        bracket_depth = 0

        while i < len(working_expr):
            char = working_expr[i]

            if char in '[{':
                bracket_depth += 1
            elif char in ']}':
                bracket_depth -= 1

            # The two halves of a conditional.  Outside one, ':' belongs to a
            # slice or a package scope and '?' to a don't-care literal, so they
            # are only separators where a conditional was found.
            if conditional and bracket_depth == 0 and char in '?:' \
                    and working_expr[i:i + 2] != '::' \
                    and working_expr[i - 1:i + 1] != '::':
                if current_token:
                    tokens.append(current_token)
                    current_token = ""
                tokens.append(char)
                i += 1
                continue

            # Handle whitespace
            if char.isspace():
                if current_token:
                    tokens.append(current_token)
                    current_token = ""
                i += 1
                continue

            # Handle parentheses - they are separate tokens
            if char == '(':
                if current_token:
                    tokens.append(current_token)
                    current_token = ""
                tokens.append('(')
                i += 1
                continue

            if char == ')':
                if current_token:
                    tokens.append(current_token)
                    current_token = ""
                tokens.append(')')
                i += 1
                continue

            # Handle equivalence operator <-> before the comparison '<' is read
            # as part of an operand: 'a <-> b' used to tokenize as the operand
            # 'a<' implying 'b', which is a literal naming no signal and the
            # wrong connective.
            if char == '<' and working_expr[i + 1:i + 3] == '->':
                if current_token:
                    tokens.append(current_token)
                    current_token = ""
                tokens.append('<->')
                i += 3
                continue

            # Handle implication operator ->
            if char == '-' and i + 1 < len(working_expr) and working_expr[i + 1] == '>':
                if current_token:
                    tokens.append(current_token)
                    current_token = ""
                tokens.append('->')
                i += 2
                continue

            # Handle AND operator &&
            if char == '&' and i + 1 < len(working_expr) and working_expr[i + 1] == '&':
                if current_token:
                    tokens.append(current_token)
                    current_token = ""
                tokens.append('&&')
                i += 2
                continue

            # Handle OR operator ||
            if char == '|' and i + 1 < len(working_expr) and working_expr[i + 1] == '|':
                if current_token:
                    tokens.append(current_token)
                    current_token = ""
                tokens.append('||')
                i += 2
                continue

            # Handle NOT operator ! - BUT only if it's not part of a larger token
            if char == '!':
                # '!=' and '!==' are comparisons: splitting off the '!' turned
                # 'divisor_i != 0' into a negation of '=0'.
                if i + 1 < len(working_expr) and working_expr[i + 1] == '=':
                    current_token += char
                    i += 1
                    continue
                # Check if this is part of a preserved expression placeholder
                if current_token.startswith('__TERM_'):
                    current_token += char
                    i += 1
                    continue
                # Otherwise treat as separate NOT operator
                if current_token:
                    tokens.append(current_token)
                    current_token = ""
                tokens.append('!')
                i += 1
                continue

            # Handle single & or | (these might be reduction operators or part of terms)
            if char in '&|':
                # Look ahead to see if this is a reduction operator
                if i + 1 < len(working_expr) and working_expr[i + 1].isalpha():
                    # This looks like a reduction operator - include it in current token
                    current_token += char
                    i += 1
                    continue
                else:
                    # Single & or | - could be part of a complex expression
                    # Include it in current token to preserve complex expressions
                    current_token += char
                    i += 1
                    continue

            # Everything else becomes part of current token
            current_token += char
            i += 1

        # Add final token if exists
        if current_token:
            tokens.append(current_token)

        # Step 4: Restore placeholders.  A placeholder can end up embedded in a
        # larger token — 'mask_q[__TERM_0__]' — so substitute inside tokens
        # rather than only replacing tokens that are a placeholder outright;
        # otherwise the placeholder text itself reaches the clause list.
        def restore(token: str) -> str:
            for _ in range(len(replacements) + 1):
                if '__TERM_' not in token:
                    break
                token = self._PLACEHOLDER_RE.sub(
                    lambda m: replacements.get(m.group(0), m.group(0)), token)
            return token

        final_tokens = [restore(token) for token in tokens]

        # Step 5: Clean up tokens - remove empty tokens.  Parentheses are kept:
        # they carry the grouping, and dropping them makes '(a||b)&&c' and
        # 'a||(b&&c)' tokenize identically.
        cleaned_tokens = []
        for token in final_tokens:
            token = token.strip()
            if token:
                # Keep operators distinct - no normalization to preserve difference
                # between logical operators (&&, ||) and reduction operators (&, |)
                cleaned_tokens.append(token)

        return cleaned_tokens

    @staticmethod
    def _ternary_present(expression: str) -> bool:
        """True when a '?' and a later ':' both sit outside any index or range.

        Both are required: a don't-care literal such as 4'b1?0 carries a '?'
        without being a conditional, and a lone ':' is a slice or a scope.
        """
        depth = 0
        question = False
        i = 0
        while i < len(expression):
            char = expression[i]
            if char in '[{':
                depth += 1
            elif char in ']}':
                depth -= 1
            elif depth == 0 and char == '?':
                question = True
            elif depth == 0 and char == ':' and question:
                if expression[i:i + 2] == '::' or expression[i - 1:i + 1] == '::':
                    i += 1
                    continue
                return True
            i += 1
        return False

    @classmethod
    def _expand_ternaries(cls, tokens: List[str]) -> Optional[List[str]]:
        """Rewrite 'c ? a : b' as '((c) && (a)) || ((!(c)) && (b))'.

        A conditional standing for the property was the largest family the
        converter refused.  With boolean arms the rewrite is exact, so the
        clauses come out of the same pipeline as any other property; the
        condition appears twice, which costs nothing because a clause holds
        operand text.  None means the conditional is malformed.
        """
        for _ in range(len(tokens)):
            if '?' not in tokens:
                return tokens
            tokens = cls._rewrite_first_ternary(tokens)
            if tokens is None:
                return None
        return tokens

    @staticmethod
    def _rewrite_first_ternary(tokens: List[str]) -> Optional[List[str]]:
        question = tokens.index('?')

        # The condition reaches back to the enclosing group or to the
        # implication, whichever comes first: those are the only tokens that
        # bind less tightly than the conditional.
        start, relative = 0, 0
        for index in range(question - 1, -1, -1):
            token = tokens[index]
            if token == ')':
                relative += 1
            elif token == '(':
                relative -= 1
                if relative < 0:
                    start = index + 1
                    break
            elif token == '->' and relative == 0:
                start = index + 1
                break

        colon, relative, nested = None, 0, 0
        for index in range(question + 1, len(tokens)):
            token = tokens[index]
            if token == '(':
                relative += 1
            elif token == ')':
                relative -= 1
                if relative < 0:
                    break
            elif relative == 0 and token == '?':
                nested += 1
            elif relative == 0 and token == ':':
                if nested:
                    nested -= 1
                else:
                    colon = index
                    break
        if colon is None:
            return None

        end, relative = len(tokens), 0
        for index in range(colon + 1, len(tokens)):
            token = tokens[index]
            if token == '(':
                relative += 1
            elif token == ')':
                relative -= 1
                if relative < 0:
                    end = index
                    break
            elif token == '->' and relative == 0:
                end = index
                break

        condition = tokens[start:question]
        if_true = tokens[question + 1:colon]
        if_false = tokens[colon + 1:end]
        if not condition or not if_true or not if_false:
            return None

        return (tokens[:start]
                + ['(', '('] + condition + [')', '&&', '('] + if_true + [')', ')']
                + ['||', '(', '!', '('] + condition + [')', '&&', '(']
                + if_false + [')', ')']
                + tokens[end:])

    def _expression_tree_from_tokens(self, tokens: List[str]) -> Tuple[Optional[Any], List[str]]:
        """
        Shunting-yard + postfix evaluation: same as the former convert_to_cnf core,
        but returns the boolean tree (no CNF flattening).  Used by convert_to_cnf
        and by NOT-on-string De Morgan expansion.

        Returns (tree, postcondition_token_list).  tree is None on failure.
        """
        operators = ('&&', '||', '!', '->', '<->', '~')
        # '!' is unary and '->' groups to the right, so an operator of equal
        # precedence must not be popped before them: popping made '!!a' produce
        # a postfix stream the evaluator could not consume.
        right_associative = ('!', '->', '<->')

        def precedence(op):
            # '<->' binds tighter than '->' although SystemVerilog gives them
            # the same level: the '->' in this token stream is almost always
            # the property implication the extractor rewrote from '|->', which
            # is looser than any boolean operator.  Reading 'a <-> b |=> c' as
            # 'a <-> (b -> c)' would move the consequent inside the antecedent.
            precedence_map = {
                "!": 4,
                "&&": 3,
                "||": 2,
                "<->": 1,
                "->": 0,
                "~": 1,
            }
            return precedence_map.get(op, -1)

        def to_postfix(toks: List[str]):
            output = []
            stack = []
            start = 0
            postcondition: List[str] = []
            for i, item in enumerate(toks):
                if item == '->':
                    start = i + 1
                    break
            if start > 0 and start < len(toks):
                postcondition = toks[start:]

            for token in toks:
                if token in operators:
                    while stack and stack[-1] != "(" and (
                        precedence(stack[-1]) > precedence(token)
                        or (precedence(stack[-1]) == precedence(token)
                            and token not in right_associative)
                    ):
                        output.append(stack.pop())
                    stack.append(token)
                elif token == "(":
                    stack.append(token)
                elif token == ")":
                    while stack and stack[-1] != "(":
                        output.append(stack.pop())
                    if not stack:
                        # Unbalanced: refuse instead of guessing a grouping.
                        return None, postcondition
                    stack.pop()
                else:
                    output.append(token)
            while stack:
                operator = stack.pop()
                if operator == "(":
                    return None, postcondition
                output.append(operator)
            return output, postcondition

        def evaluate_postfix(postfix: List[str]) -> Optional[Any]:
            if postfix is None:
                return None
            stack = []
            try:
                for token in postfix:
                    if token == "!":
                        if not stack:
                            return None
                        operand = stack.pop()
                        stack.append(["NOT", operand])
                    elif token == "&&":
                        if len(stack) < 2:
                            return None
                        right = stack.pop()
                        left = stack.pop()
                        stack.append(["AND", left, right])
                    elif token == "||":
                        if len(stack) < 2:
                            return None
                        right = stack.pop()
                        left = stack.pop()
                        stack.append(["OR", left, right])
                    elif token == "->":
                        if len(stack) < 2:
                            return None
                        right = stack.pop()
                        left = stack.pop()
                        stack.append(["IMPLIES", left, right])
                    elif token in ("<->", "~"):
                        if len(stack) < 2:
                            return None
                        right = stack.pop()
                        left = stack.pop()
                        stack.append(["EQUIV", left, right])
                    elif len(token) > 1 and token[0] in '&|':
                        operator = token[0]
                        operand = token[1:]
                        stack.append(["REDUCTION", operator, operand])
                    else:
                        # Do not strip trailing ')' — that was corrupting consequents like
                        # (ptw.next_state==ptw.S_REQ) into unbalanced text.
                        stack.append(token)
                # Operands left over mean the expression was not fully
                # connected; returning stack[0] discarded the rest, so a
                # property could silently collapse to one of its operands.
                return stack[0] if len(stack) == 1 else None
            except IndexError:
                return None

        postfix, postcondition = to_postfix(tokens)
        if postfix is None:
            return None, postcondition
        tree = evaluate_postfix(postfix)
        return tree, postcondition

    def convert_to_cnf(self, tokens: List[str]) -> Tuple[List[List[Union[str, bool]]], List[str]]:
        """
        Convert a tokenized SVA expression into CNF form.

        :param tokens: List of tokens from an SVA expression.
        :return: Tuple of (CNF clauses, postcondition tokens).
        """
        tokens = self._expand_ternaries(tokens)
        if tokens is None:
            return None, None
        result, postcondition = self._expression_tree_from_tokens(tokens)
        if result is None:
            return None, None
        clauses = self.flatten_to_cnf(result)
        if clauses is None:
            return None, None
        return clauses, postcondition

    def flatten_to_cnf(self, expr: Any) -> Optional[List[List[str]]]:
        """
        Flatten a boolean expression tree into CNF clauses.

        The conversion is exact: implications are eliminated, negations are
        pushed down to the literals, and OR is distributed over AND until the
        tree is a conjunction of disjunctions of literals.  When a faithful
        clause list cannot be produced — a leaf that is not an atom, or a
        distribution that would explode — the method returns None so the caller
        can record the property as unprocessable.  It never returns an
        approximation, because the clause list is compared for equality to
        decide whether the model produced a new property.

        :param expr: Logical expression tree.
        :return: CNF clauses, or None when the tree cannot be represented.
        """
        def literal_to_str(node) -> Optional[str]:
            """Atom text for a leaf, or None when the leaf is not an atom."""
            if isinstance(node, str):
                return self._canonical_atom(node)
            if isinstance(node, list) and node and node[0] == "REDUCTION":
                return f"{node[1]}{node[2]}"
            return None

        def eliminate(node):
            """Rewrite IMPLIES and EQUIV in terms of AND, OR and NOT."""
            if not isinstance(node, list) or not node:
                return node
            op = node[0]
            if op == "IMPLIES":
                return ["OR", ["NOT", eliminate(node[1])], eliminate(node[2])]
            if op == "EQUIV":
                left, right = eliminate(node[1]), eliminate(node[2])
                return ["AND",
                        ["OR", ["NOT", left], right],
                        ["OR", ["NOT", right], left]]
            if op in ("AND", "OR"):
                return [op] + [eliminate(child) for child in node[1:]]
            if op == "NOT":
                return ["NOT", eliminate(node[1])]
            return node

        def push_negations(node, negated=False):
            """Negation normal form: NOT survives only on literals."""
            if isinstance(node, list) and node and node[0] in ("AND", "OR"):
                flipped = {"AND": "OR", "OR": "AND"}[node[0]] if negated \
                    else node[0]
                return [flipped] + [push_negations(child, negated)
                                    for child in node[1:]]
            if isinstance(node, list) and node and node[0] == "NOT":
                return push_negations(node[1], not negated)
            return ["NOT", node] if negated else node

        def flatten(node):
            """Merge an AND into a parent AND, and an OR into a parent OR."""
            if not isinstance(node, list) or not node \
                    or node[0] not in ("AND", "OR"):
                return node
            op = node[0]
            children = []
            for child in (flatten(child) for child in node[1:]):
                if isinstance(child, list) and child and child[0] == op:
                    children.extend(child[1:])
                else:
                    children.append(child)
            if len(children) == 1:
                return children[0]
            return [op] + children

        def clause_count(node) -> int:
            """Number of clauses the distribution would produce."""
            if not isinstance(node, list) or not node \
                    or node[0] not in ("AND", "OR"):
                return 1
            counts = [clause_count(child) for child in node[1:]]
            if node[0] == "AND":
                return sum(counts)
            product = 1
            for count in counts:
                product *= count
                if product > MAX_CNF_CLAUSES:
                    return product
            return product

        def distribute(node):
            """Push OR inside AND so the result is a conjunction of clauses."""
            if not isinstance(node, list) or not node \
                    or node[0] not in ("AND", "OR"):
                return node
            children = [distribute(child) for child in node[1:]]
            if node[0] == "AND":
                return flatten(["AND"] + children)
            conjunction = next(
                (i for i, child in enumerate(children)
                 if isinstance(child, list) and child and child[0] == "AND"),
                None)
            if conjunction is None:
                return flatten(["OR"] + children)
            others = children[:conjunction] + children[conjunction + 1:]
            expanded = [distribute(flatten(["OR"] + others + [term]))
                        for term in children[conjunction][1:]]
            return flatten(["AND"] + expanded)

        def literal_of(node) -> Optional[str]:
            if isinstance(node, list) and node and node[0] == "NOT":
                inner = literal_to_str(node[1])
                return None if inner is None else "!" + inner
            return literal_to_str(node)

        def collect_clauses(node) -> Optional[List[List[str]]]:
            if isinstance(node, list) and node and node[0] == "AND":
                clauses = []
                for child in node[1:]:
                    part = collect_clauses(child)
                    if part is None:
                        return None
                    clauses.extend(part)
                return clauses

            terms = node[1:] if (isinstance(node, list) and node
                                 and node[0] == "OR") else [node]
            literals = []
            for term in terms:
                literal = literal_of(term)
                if literal is None:
                    return None
                # A disjunction repeating an operand says nothing twice, and
                # distribution produces repeats whenever a condition appears in
                # both arms of a conditional.
                if literal not in literals:
                    literals.append(literal)
            return [literals]

        if expr is None:
            return None

        normalised = flatten(push_negations(eliminate(expr)))
        if clause_count(normalised) > MAX_CNF_CLAUSES:
            return None
        return collect_clauses(distribute(normalised))

    def equivalence_elimination(self, clauses):
        """
        Perform equivalence-based elimination on stored clauses.

        :return: Reduced set of clauses.
        """
        variable_map = {}
        reduced_clauses = []

        for clause in clauses:
            if isinstance(clause, list) and len(clause) == 2 and clause[0] == "EQUIV":
                var, equivalent = clause[1:]
                variable_map[var] = equivalent
            else:
                reduced_clauses.append(clause)

        # Replace variables with their equivalents in reduced_clauses
        def replace_variables(clause):
            if isinstance(clause, list):
                return [replace_variables(lit) for lit in clause]
            return variable_map.get(clause, clause)

        reduced_clauses = [replace_variables(clause) for clause in reduced_clauses]

        clauses = reduced_clauses
        return reduced_clauses

    @staticmethod
    def clause_signature(clauses):
        """Order-independent signature of a clause list.

        A clause is a disjunction and the list is a conjunction, so neither
        order carries meaning: 'req && ack' and 'ack && req' are one property,
        and comparing the printed lists counted them as two.
        """
        return frozenset(
            frozenset(re.sub(r'\s+', '', str(literal)) for literal in clause)
            for clause in clauses)

    @staticmethod
    def postcondition_signature(postcondition):
        """Signature of a postcondition: where the delay sits, then its tokens.

        The token order is dropped for the same reason as in the clause list;
        the leading delay marker is kept exactly, because it is the only record
        of whether the delay belongs to the consequent.
        """
        tokens = [SVAParser._canonical_atom(re.sub(r'\s+', '', str(token)))
                  for token in (postcondition or [])]
        if tokens and tokens[0].startswith('##'):
            return tokens[0], tuple(sorted(tokens[1:]))
        return '', tuple(sorted(tokens))

    def exists_property(self, prop_name, prop_delay, prop_clauses,
                        prop_postcondition, sva_property=None):
        """
        Check if a property already exists in our database.
        For unprocessable properties, we compare the assert property text.
        For normal properties, we follow the comparison order: delay -> clauses -> postcondition.

        Args:
            prop_name: Name of the property
            prop_delay: Delay value of the property
            prop_clauses: Clauses of the property
            prop_postcondition: Postcondition of the property
            sva_property: Full assertion text, used for unprocessable properties

        Returns:
            bool: True if property exists, False otherwise
        """
        # First check if this is an unprocessable property
        if prop_clauses is None:
            # No clause list to compare, so fall back to the property text.
            # Comparing names alone let a relabelled copy of the same
            # unparsable property count as a new property and keep the
            # extension loop running.
            if sva_property is not None:
                canonical = self.canonical_property_text(sva_property)
                exists = canonical in self.unprocessable_bodies
            else:
                exists = prop_name in self.unprocessable_props_dict
            if self.debug_mode:
                print(f"  Checking unprocessable property '{prop_name}': {'EXISTS' if exists else 'NEW'}")
            return exists

        if self.debug_mode:
            print(f"  Checking property '{prop_name}' with delay={prop_delay}")
            print(f"  New clauses: {prop_clauses}")
            print(f"  New postcondition: {prop_postcondition}")

        # For normal properties, follow the comparison order: delay -> clauses -> postcondition
        for n, existing_clauses in self.clauses_d.items():
            # Skip if comparing with an unprocessable property
            if n in self.unprocessable_props_dict:
                continue

            if self.debug_mode:
                print(f"    Comparing with existing property '{n}':")
                print(f"      Existing delay: {self.delay_d[n]}")
                print(f"      Existing clauses: {existing_clauses}")
                print(f"      Existing postcondition: {self.postcondition_d[n]}")

            # First check delay - if delays are different, it's a different property
            if prop_delay != self.delay_d[n]:
                if self.debug_mode:
                    print(f"      -> Different delays ({prop_delay} vs {self.delay_d[n]})")
                continue

            # Second check clauses - if clauses are different, it's a different property
            try:
                new_clauses_str = self.clause_signature(prop_clauses)
                existing_clauses_str = self.clause_signature(existing_clauses)

                if self.debug_mode:
                    print(f"      Normalized new clauses: {new_clauses_str}")
                    print(f"      Normalized existing clauses: {existing_clauses_str}")

                if new_clauses_str != existing_clauses_str:
                    if self.debug_mode:
                        print(f"      -> Different clauses")
                    continue
            except Exception as e:
                if self.debug_mode:
                    print(f"      -> Error comparing clauses: {e}")
                continue

            # Third check postcondition - if postcondition is different, it's a different property
            try:
                norm_new_post = self.postcondition_signature(prop_postcondition)
                norm_existing_post = self.postcondition_signature(
                    self.postcondition_d[n])

                if self.debug_mode:
                    print(f"      Normalized new postcondition: '{norm_new_post}'")
                    print(f"      Normalized existing postcondition: '{norm_existing_post}'")

                if norm_new_post != norm_existing_post:
                    if self.debug_mode:
                        print(f"      -> Different postconditions")
                    continue
            except Exception as e:
                if self.debug_mode:
                    print(f"      -> Error comparing postconditions: {e}")
                continue

            # If we reach here, delay, clauses, and postcondition all match
            # This means it's the same property
            if self.debug_mode:
                print(f"      -> DUPLICATE FOUND! Property matches existing '{n}'")
            return True

        if self.debug_mode:
            print(f"  -> NEW property (no duplicates found)")
        return False

    @classmethod
    def _property_name(cls, sva_property):
        """The label of an assertion, the placeholder if it has none.

        None means the text is not an assert property at all.  An unlabelled
        property used to be rejected here, and the caller reads that as 'already
        present', so a property the tool could not even name counted towards the
        'no new assertions' stop condition.
        """
        match = re.search(r'([a-zA-Z0-9_]+):\s*assert\s*property', sva_property)
        if match:
            return match.group(1)
        if re.search(r'assert\s*property', sva_property):
            return cls._SYNTHETIC_LABEL
        return None

    def _unique_name(self, original_name):
        """A name not yet used by any of the property dictionaries."""
        final_name = original_name
        name_counter = 0
        while (final_name in self.clauses_d or
               final_name in self.unprocessable_props_dict or
               final_name in self.sva_string or
               final_name in self.delay_d or
               final_name in self.postcondition_d):
            final_name = f"{original_name}_{name_counter}"
            name_counter += 1
            if self.debug_mode:
                print(f"    Name collision detected, trying: '{final_name}'")
        return final_name

    def _store_unprocessable(self, original_name, sva_property):
        """Store a property the CNF pipeline cannot represent.

        Returns 1 when it is new, 0 when the same assert property text is
        already recorded under any label.
        """
        canonical = self.canonical_property_text(sva_property)
        if canonical in self.unprocessable_bodies:
            if self.debug_mode:
                print(f"  -> Already exists as unprocessable property")
            return 0

        final_name = self._unique_name(original_name)
        self.unprocessable_properties.add(sva_property)
        self.unprocessable_props_dict[final_name] = sva_property
        self.unprocessable_bodies[canonical] = final_name
        if self.debug_mode:
            print(f"  -> Stored as new unprocessable property '{final_name}'")
        return 1

    def process_property(self, sva_property):
        """
        Process and store a property if it's new.
        Returns 1 if property was stored (new), 0 if it already exists or failed to process.
        """
        try:
            # Extract property name first - handle both normalized and non-normalized formats
            original_name = self._property_name(sva_property)
            if original_name is None:
                if self.debug_mode:
                    print("  -> Not an assert property")
                return 0

            if self.debug_mode:
                print(f"\nProcessing property with original name: '{original_name}'")

            # Try to parse the property
            try:
                parsed_name, delay, clauses, postcondition = self.parse_sva(sva_property)

                if clauses is None:
                    # Property is unprocessable
                    if self.debug_mode:
                        print(f"  Property is unprocessable")
                    return self._store_unprocessable(original_name, sva_property)

                # Check if this property already exists (by delay, clauses, postcondition)
                if self.exists_property(original_name, delay, clauses,
                                        postcondition, sva_property):
                    if self.debug_mode:
                        print(f"  -> Property already exists (duplicate)")
                    return 0  # Property already exists

                # Property is new, need to handle potential name collision
                if self.debug_mode:
                    print(f"  Property is new, processing...")

                # Apply equivalence elimination
                eliminated_clauses = self.equivalence_elimination(clauses)
                if self.debug_mode:
                    print(f"  Clauses after equivalence elimination: {eliminated_clauses}")

                # Find a unique name - ALWAYS ensure uniqueness, even for different properties
                final_name = self._unique_name(original_name)

                # Store the new property
                self.sva_string[final_name] = sva_property
                self.delay_d[final_name] = delay
                self.clauses_d[final_name] = eliminated_clauses  # Store the eliminated clauses
                self.postcondition_d[final_name] = postcondition

                if final_name != original_name:
                    if self.debug_mode:
                        print(f'  -> Property name collision resolved: {original_name} -> {final_name}')
                else:
                    if self.debug_mode:
                        print(f'  -> Property stored with original name: {final_name}')

                return 1  # Successfully stored new property

            except Exception as e:
                # If parsing fails, store as unprocessable
                if self.debug_mode:
                    print(f"  Error parsing property: {e}")
                return self._store_unprocessable(original_name, sva_property)

        except Exception as e:
            print(f"Error processing property: {e}")
        return 0

    def property_signature(self, sva_property):
        """The identity of a property, as a value, without storing anything.

        ``exists_property`` answers the same question against the database.  A
        caller that has several candidates in hand needs to compare them with
        each other before any of them is stored, which is what this is for.
        Two properties share a signature exactly when the database would call
        the second one a duplicate of the first.  None means the text is not an
        assert property.
        """
        if self._property_name(sva_property) is None:
            return None
        try:
            _name, delay, clauses, postcondition = self.parse_sva(sva_property)
        except Exception:
            clauses = None
        if clauses is None:
            return ('text', self.canonical_property_text(sva_property))
        return ('cnf', delay, self.clause_signature(clauses),
                self.postcondition_signature(postcondition))

    def check_property_duplicate(self, sva_property):
        """
        Check if a property is a duplicate without storing it.
        This is used for pre-syntax duplicate checking.

        Args:
            sva_property: SVA property string to check

        Returns:
            bool: True if property is a duplicate, False if it's new or unparseable
        """
        try:
            # Extract property name first - handle both normalized and non-normalized formats
            original_name = self._property_name(sva_property)
            if original_name is None:
                return False  # Not an assertion, so nothing to have seen before

            # Try to parse the property
            try:
                parsed_name, delay, clauses, postcondition = self.parse_sva(sva_property)

                if clauses is None:
                    # Property is unprocessable, check if we already have it
                    return (self.canonical_property_text(sva_property)
                            in self.unprocessable_bodies)

                # Check if this property already exists (by delay, clauses, postcondition)
                return self.exists_property(original_name, delay, clauses,
                                            postcondition, sva_property)

            except Exception as e:
                # If parsing fails, check if we already have it as unprocessable
                return (self.canonical_property_text(sva_property)
                        in self.unprocessable_bodies)

        except Exception as e:
            print(f"Error checking property duplicate: {e}")
            return False  # If we can't check, assume it's new

    def validate_no_duplicates(self):
        """
        Validate that there are no duplicate properties in the stored data.
        Returns a report of any issues found.
        """
        print("\n=== DUPLICATE VALIDATION REPORT ===")

        # Check for consistency across dictionaries (same names should appear in all)
        processable_names = set(self.clauses_d.keys())
        unprocessable_names = set(self.unprocessable_props_dict.keys())

        # All processable properties should have entries in all related dictionaries
        missing_entries = []
        for name in processable_names:
            if name not in self.sva_string:
                missing_entries.append(f"Property '{name}' missing from sva_string")
            if name not in self.delay_d:
                missing_entries.append(f"Property '{name}' missing from delay_d")
            if name not in self.postcondition_d:
                missing_entries.append(f"Property '{name}' missing from postcondition_d")

        # Check for orphaned entries
        orphaned_entries = []
        for dict_name, dictionary in [
            ("sva_string", self.sva_string),
            ("delay_d", self.delay_d),
            ("postcondition_d", self.postcondition_d)
        ]:
            for name in dictionary.keys():
                if name not in processable_names and name not in unprocessable_names:
                    orphaned_entries.append(f"Orphaned entry '{name}' in {dict_name}")

        # Check for content duplicates among processable properties
        content_duplicates = []
        processed_properties = []

        for name, clauses in self.clauses_d.items():
            delay = self.delay_d[name]
            postcondition = self.postcondition_d[name]

            # Create a signature for this property, using the same
            # order-independent comparison that decided it was new
            signature = (delay, self.clause_signature(clauses),
                         self.postcondition_signature(postcondition))

            for other_name, other_sig in processed_properties:
                if signature == other_sig and name != other_name:
                    content_duplicates.append(f"Properties '{name}' and '{other_name}' have identical content")

            processed_properties.append((name, signature))

        # Check for name collisions between processable and unprocessable
        name_collisions = []
        for name in processable_names:
            if name in unprocessable_names:
                name_collisions.append(f"Name '{name}' exists in both processable and unprocessable properties")

        # Report results
        issues_found = False

        if missing_entries:
            print("MISSING ENTRIES FOUND:")
            for entry in missing_entries:
                print(f"  - {entry}")
            issues_found = True
        else:
            print("✓ No missing entries found")

        if orphaned_entries:
            print("\nORPHANED ENTRIES FOUND:")
            for entry in orphaned_entries:
                print(f"  - {entry}")
            issues_found = True
        else:
            print("✓ No orphaned entries found")

        if content_duplicates:
            print("\nCONTENT DUPLICATES FOUND:")
            for duplicate in content_duplicates:
                print(f"  - {duplicate}")
            issues_found = True
        else:
            print("✓ No content duplicates found")

        if name_collisions:
            print("\nNAME COLLISIONS FOUND:")
            for collision in name_collisions:
                print(f"  - {collision}")
            issues_found = True
        else:
            print("✓ No name collisions found")

        print(f"\nSUMMARY:")
        print(f"  Total processable properties: {len(processable_names)}")
        print(f"  Total unprocessable properties: {len(unprocessable_names)}")
        print(f"  Total unique names: {len(processable_names) + len(unprocessable_names)}")

        return not issues_found
