#!/usr/bin/env python3
"""Contract tests for the assertion database at the heart of SVApshot.

The parser decides whether the model produced a new property, and the extension
loop stops when a batch adds none.  A clause list is therefore not a summary but
an identity: two properties that share a tuple are held to be the same property.

The suite checks that identity from both sides.  Logical faithfulness is
established by truth table against a reference parser written here, independent
of the tokenizer under test, so a wrong grouping cannot pass by agreeing with
itself.  Operand handling is checked literally, because an operand silently
truncated to a prefix makes unrelated properties collide.  No language model is
involved: every case is a fixed string.
"""

from __future__ import annotations

import itertools
import os
import re
import sys
import unittest

import _path_setup  # noqa: F401, E402

import agent as agent_module
import svaparser
from svaparser import SVAParser


# ─────────────────────────────────────────────────────────────────────
#  Reference boolean parser — deliberately independent of svaparser
# ─────────────────────────────────────────────────────────────────────

def reference_tree(text: str):
    """Parse a boolean body whose atoms are plain names.

    Written from the operator table alone (! binds tightest, then &&, then ||,
    then <->, then ->, with parentheses grouping) so that comparing against it
    is a real check rather than a restatement of the implementation.
    """
    tokens = re.findall(
        r'<->|->|\|\||&&|!|\(|\)|\?|:|[A-Za-z_][A-Za-z0-9_]*', text)
    position = [0]

    def peek():
        return tokens[position[0]] if position[0] < len(tokens) else None

    def take():
        token = peek()
        position[0] += 1
        return token

    def primary():
        token = take()
        if token == '(':
            node = implication()
            if take() != ')':
                raise ValueError(f'unbalanced parentheses in {text!r}')
            return node
        if token == '!':
            return ('not', primary())
        if token is None or token in ('&&', '||', '->', '<->', ')'):
            raise ValueError(f'expected an operand in {text!r}')
        return ('atom', token)

    def conjunction():
        node = primary()
        while peek() == '&&':
            take()
            node = ('and', node, primary())
        return node

    def disjunction():
        node = conjunction()
        while peek() == '||':
            take()
            node = ('or', node, conjunction())
        return node

    def conditional():
        node = disjunction()
        if peek() != '?':
            return node
        take()
        if_true = conditional()
        if take() != ':':
            raise ValueError(f'conditional without an else arm in {text!r}')
        return ('ite', node, if_true, conditional())

    def equivalence():
        node = conditional()
        while peek() == '<->':
            take()
            node = ('iff', node, conditional())
        return node

    def implication():
        node = equivalence()
        if peek() == '->':
            take()
            return ('implies', node, implication())
        return node

    tree = implication()
    if position[0] != len(tokens):
        raise ValueError(f'trailing tokens in {text!r}')
    return tree


def evaluate(node, values):
    kind = node[0]
    if kind == 'atom':
        return values[node[1]]
    if kind == 'not':
        return not evaluate(node[1], values)
    if kind == 'and':
        return evaluate(node[1], values) and evaluate(node[2], values)
    if kind == 'or':
        return evaluate(node[1], values) or evaluate(node[2], values)
    if kind == 'implies':
        return (not evaluate(node[1], values)) or evaluate(node[2], values)
    if kind == 'iff':
        return evaluate(node[1], values) == evaluate(node[2], values)
    if kind == 'ite':
        return (evaluate(node[2], values) if evaluate(node[1], values)
                else evaluate(node[3], values))
    raise AssertionError(f'unknown node {kind}')


def evaluate_clauses(clauses, values):
    """Truth value of a clause list under *values*."""
    for clause in clauses:
        satisfied = False
        for literal in clause:
            literal = str(literal)
            negated = literal.startswith('!')
            name = literal[1:] if negated else literal
            value = values[name]
            satisfied = satisfied or (not value if negated else value)
        if not satisfied:
            return False
    return True


def clause_set(clauses):
    """Clause list as a set of sets, so ordering does not matter."""
    return frozenset(frozenset(str(l) for l in clause) for clause in clauses)


def parse(body: str, name: str = 'a_probe'):
    """Parse an assertion built around *body* and return the tuple."""
    return SVAParser().parse_sva(f'{name}: assert property ({body});')


def clauses_of(body: str):
    return parse(body)[2]


def literals_of(clauses):
    return [str(literal) for clause in clauses for literal in clause]


def atom_of(literal: str) -> str:
    return literal[1:] if literal.startswith('!') else literal


#: Bodies whose atoms are plain names, so the reference parser can read them.
BOOLEAN_BODIES = [
    'a',
    '!a',
    '!!a',
    'a&&b',
    'a||b',
    'a->b',
    '!a->b',
    'a->!b',
    '(a&&b)->c',
    'a->(b&&c)',
    '(a||b)->c',
    'a->(b||c)',
    '!(a&&b)',
    '!(a||b)',
    '!(a&&b)->c',
    '!(a||b)->c',
    '(a&&b)->(c||d)',
    '(a||b)->(c&&d)',
    '(a||b)&&c->d',
    '((a||b)&&c)->d',
    'a&&(b||c)->d',
    '(a&&b)||(c&&d)->e',
    '(a&&(b||c))->(d&&e)',
    '!(a&&(b||c))->d',
    '(a||b)&&(c||d)->e',
    'a&&b&&c->d',
    'a||b||c->d',
    'a->b->c',
    '!(!a||!b)->c',
    '(a->b)&&c->d',
    'c?a:b',
    '!c?a:b',
    'c?a:b->d',
    'a->(c?b:d)',
    '(a&&b)?c:d',
    'c?(a&&b):(d||e)',
    'c?a:(d?e:f)',
    '(c?a:b)&&d->e',
    'a<->b',
    '!a<->b',
    'a<->!b',
    '!(a<->b)',
    'a<->b->c',
    'a&&b<->c',
    'a<->b&&c',
    '(a||b)<->(c&&d)',
    'a->(b<->c)',
    '(a<->b)&&c->d',
]

#: Assertions in the shape the flow actually produces, used for the invariants
#: that must hold for every property regardless of its logic.
REALISTIC_ASSERTIONS = [
    'a_handshake: assert property (@(posedge clk_i) disable iff (!rst_ni) '
    'req_i |=> gnt_o);',
    'a_delayed: assert property (@(posedge clk_i) req_i |-> ##2 gnt_o);',
    'a_state: assert property (div_unit.state_q == IDLE |-> !div_unit.busy_q);',
    "a_literal: assert property (data_q == 32'd0 |=> zero_o);",
    'a_slice: assert property (data_q[31:0] == data_i[31:0] |-> valid_o);',
    'a_past: assert property (valid_i |=> data_o == $past(data_i));',
    'a_rose: assert property ($rose(req_i) |=> gnt_o);',
    'a_concat: assert property (bus_q == {high_i, low_i} |-> ok_o);',
    'a_inside: assert property ((state_q inside {IDLE, RUN}) |-> !err_o);',
    'a_paren: assert property ((next_state == S_REQ) |-> req_o);',
    'a_disjunction: assert property (req_i && (state_q == IDLE || '
    'state_q == WAIT) |=> gnt_o);',
    'a_arith: assert property (count_q == (limit_q - 1) |=> wrap_o);',
    'a_reduction: assert property (|mask_i |-> any_o);',
    'a_demorgan: assert property (!(full_q && push_i) |-> !overflow_o);',
    'a_indexed: assert property (mask_q[func(idx_i)] |-> ok_o);',
    'a_scoped: assert property (opgrp_q[pkg::get_group(op_i)] |=> ready_o);',
]


# ─────────────────────────────────────────────────────────────────────
#  Structural extraction
# ─────────────────────────────────────────────────────────────────────

class TestStructuralExtraction(unittest.TestCase):
    """Label, clock, reset guard and delay are read off the assertion."""

    def test_the_label_becomes_the_property_name(self):
        name, _delay, _clauses, _post = parse('req_i |=> gnt_o', 'a_named')
        self.assertEqual(name, 'a_named')

    def test_the_clock_event_does_not_reach_the_clauses(self):
        clauses = SVAParser().parse_sva(
            'a_clk: assert property (@(posedge clk_i) req_i |=> gnt_o);')[2]
        self.assertEqual(clause_set(clauses),
                         clause_set([['!req_i', 'gnt_o']]))

    def test_the_reset_guard_does_not_reach_the_clauses(self):
        clauses = SVAParser().parse_sva(
            'a_rst: assert property (@(posedge clk_i) disable iff (!rst_ni) '
            'req_i |=> gnt_o);')[2]
        self.assertEqual(clause_set(clauses),
                         clause_set([['!req_i', 'gnt_o']]))

    def test_an_overlapping_implication_carries_no_delay(self):
        self.assertEqual(parse('req_i |-> gnt_o')[1], 0)

    def test_a_non_overlapping_implication_carries_one_cycle(self):
        self.assertEqual(parse('req_i |=> gnt_o')[1], 1)

    def test_an_explicit_delay_is_summed(self):
        self.assertEqual(parse('req_i |-> ##2 gnt_o')[1], 2)

    def test_a_non_overlapping_implication_adds_to_its_own_delay(self):
        self.assertEqual(parse('req_i |=> ##2 gnt_o')[1], 3)

    def test_the_postcondition_records_where_the_delay_sits(self):
        post = parse('req_i |-> ##2 gnt_o')[3]
        self.assertEqual(post[0], '##2')

    def test_an_antecedent_delay_is_not_charged_to_the_consequent(self):
        _name, delay, _clauses, post = parse('a_i ##2 b_i |-> c_o')
        self.assertEqual(delay, 2)
        self.assertEqual(post[0], '##0')

    def test_an_unlabelled_property_is_parsed_and_reports_no_name(self):
        # The grammar requires a label, so this used to raise out of the regex
        # fallback and be quarantined as a 'parser exception' after two LLM
        # repair attempts, though the property itself is representable.
        name, delay, clauses, _post = SVAParser().parse_sva(
            'assert property (@(posedge clk_i) req_i |=> gnt_o);')
        self.assertEqual(name, '')
        self.assertEqual(delay, 1)
        self.assertEqual(clause_set(clauses), clause_set([['!req_i', 'gnt_o']]))

    def test_an_unlabelled_property_keeps_its_failure_action_out(self):
        clauses = SVAParser().parse_sva(
            'assert property (req_i |=> gnt_o) '
            'else begin $display("no gnt"); end')[2]
        self.assertEqual(clause_set(clauses), clause_set([['!req_i', 'gnt_o']]))

    def test_a_missing_terminator_does_not_lose_the_property(self):
        clauses = SVAParser().parse_sva('a_x: assert property (req_i |=> gnt_o)')[2]
        self.assertEqual(clause_set(clauses), clause_set([['!req_i', 'gnt_o']]))

    def test_the_whitespace_free_form_the_agent_passes_parses_the_same(self):
        spaced = 'a_ws: assert property (req_i && ack_i |=> gnt_o);'
        stripped = re.sub(r'\s+', '', spaced)
        self.assertEqual(SVAParser().parse_sva(spaced),
                         SVAParser().parse_sva(stripped))

    def test_an_interior_delay_stays_in_the_operand_text(self):
        first = clauses_of('a_i ##1 b_i |-> c_o')
        second = clauses_of('a_i ##2 b_i |-> c_o')
        self.assertNotEqual(clause_set(first), clause_set(second))


# ─────────────────────────────────────────────────────────────────────
#  Logical faithfulness of the clause list
# ─────────────────────────────────────────────────────────────────────

class TestClauseLogic(unittest.TestCase):
    """Every clause list means exactly what the assertion means."""

    def assert_equivalent(self, body: str):
        clauses = clauses_of(body)
        self.assertIsNotNone(clauses, f'{body} was rejected')
        atoms = sorted({atom_of(l) for l in literals_of(clauses)})
        expected_atoms = sorted(set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', body)))
        self.assertEqual(atoms, expected_atoms,
                         f'{body} produced literals {literals_of(clauses)}')
        reference = reference_tree(body)
        for combination in itertools.product([False, True], repeat=len(atoms)):
            values = dict(zip(atoms, combination))
            self.assertEqual(
                evaluate_clauses(clauses, values),
                evaluate(reference, values),
                f'{body} disagrees with its meaning at {values}')

    def test_every_sample_body_is_logically_equivalent_to_its_clauses(self):
        for body in BOOLEAN_BODIES:
            with self.subTest(body=body):
                self.assert_equivalent(body)

    def test_a_disjunction_inside_a_conjunction_keeps_its_grouping(self):
        # Losing the parentheses turns this into a||(b&&c), which is a
        # different property that the database would then confuse with it.
        self.assertNotEqual(clause_set(clauses_of('(a||b)&&c->d')),
                            clause_set(clauses_of('a||(b&&c)->d')))

    def test_a_negated_conjunction_expands_by_de_morgan(self):
        self.assertEqual(clause_set(clauses_of('!(a&&b)->c')),
                         clause_set([['a', 'c'], ['b', 'c']]))

    def test_a_negated_disjunction_expands_by_de_morgan(self):
        self.assertEqual(clause_set(clauses_of('!(a||b)->c')),
                         clause_set([['a', 'b', 'c']]))

    def test_a_double_negation_cancels(self):
        self.assertEqual(clause_set(clauses_of('!!a->b')),
                         clause_set(clauses_of('a->b')))

    def test_a_conjunctive_consequent_becomes_one_clause_per_conjunct(self):
        self.assertEqual(clause_set(clauses_of('a->(b&&c)')),
                         clause_set([['!a', 'b'], ['!a', 'c']]))

    def test_a_disjunctive_antecedent_becomes_one_clause_per_disjunct(self):
        self.assertEqual(clause_set(clauses_of('(a||b)->c')),
                         clause_set([['!a', 'c'], ['!b', 'c']]))

    def test_distribution_covers_every_combination(self):
        self.assertEqual(
            clause_set(clauses_of('(a||b)&&(c||d)->e')),
            clause_set([['!a', '!c', 'e'], ['!a', '!d', 'e'],
                        ['!b', '!c', 'e'], ['!b', '!d', 'e']]))

    def test_a_reduction_operator_is_one_literal(self):
        self.assertEqual(clause_set(clauses_of('|mask_i -> any_o')),
                         clause_set([['!|mask_i', 'any_o']]))

    def test_an_equivalence_is_not_read_as_an_implication(self):
        # '<->' used to lose its '<' into the operand before it, so the
        # property became 'a_i< -> b_i': a literal naming no signal, and an
        # implication where the assertion said equivalence.
        clauses = clauses_of('a<->b')
        self.assertEqual(clause_set(clauses),
                         clause_set([['!a', 'b'], ['!b', 'a']]))

    def test_an_equivalence_is_not_confused_with_a_comparison(self):
        self.assertEqual(literals_of(clauses_of('a_i<b_i->c_o')),
                         ['!a_i<b_i', 'c_o'])


# ─────────────────────────────────────────────────────────────────────
#  Operand handling
# ─────────────────────────────────────────────────────────────────────

class TestOperandsSurviveIntact(unittest.TestCase):
    """An operand reaches the clause list whole or not at all.

    A truncated operand is worse than a rejected one: 'mask_q[func' is accepted
    downstream and matches every other property over the same signal.
    """

    def assert_single_literal(self, body: str, expected: str):
        clauses = clauses_of(body)
        self.assertIsNotNone(clauses, f'{body} was rejected')
        self.assertEqual(clause_set(clauses), clause_set([[expected]]))

    def test_a_sampled_value_call_is_one_operand(self):
        self.assert_single_literal('$rose(req_i)', '$rose(req_i)')

    def test_a_call_in_an_implication_is_one_operand(self):
        self.assertEqual(clause_set(clauses_of('$rose(req_i)->gnt_o')),
                         clause_set([['!$rose(req_i)', 'gnt_o']]))

    def test_a_comparison_against_a_call_is_one_operand(self):
        self.assert_single_literal('data_o==$past(data_i)',
                                   'data_o==$past(data_i)')

    def test_a_sized_literal_comparison_is_one_operand(self):
        self.assert_single_literal("data_q==32'd0", "data_q==32'd0")

    def test_a_part_select_comparison_is_one_operand(self):
        self.assert_single_literal('data_q[31:0]==data_i[31:0]',
                                   'data_q[31:0]==data_i[31:0]')

    def test_a_concatenation_comparison_is_one_operand(self):
        self.assert_single_literal('bus_q=={high_i,low_i}',
                                   'bus_q=={high_i,low_i}')

    def test_a_parenthesised_comparison_is_one_operand(self):
        self.assert_single_literal('(next_state==S_REQ)',
                                   'next_state==S_REQ')

    def test_parentheses_around_a_whole_operand_are_dropped(self):
        self.assertEqual(clause_set(clauses_of('(state_q==IDLE)&&valid_i')),
                         clause_set(clauses_of('state_q==IDLE&&valid_i')))

    def test_a_comparison_against_a_boolean_group_is_one_operand(self):
        self.assert_single_literal('ready_o==(valid_i&&fmt_ready[fmt_i])',
                                   'ready_o==(valid_i&&fmt_ready[fmt_i])')

    def test_a_comparison_whose_left_side_is_a_group_is_one_operand(self):
        self.assert_single_literal('(remainder_q>=divisor_i)==quotient_bit',
                                   '(remainder_q>=divisor_i)==quotient_bit')

    def test_an_inequality_against_a_bare_number_is_one_operand(self):
        # Splitting the '!' off '!=' turned this into a negation of '=0'.
        self.assertEqual(clause_set(clauses_of('divisor_i!=0->valid_o')),
                         clause_set([['!divisor_i!=0', 'valid_o']]))

    def test_an_arithmetic_comparison_is_one_operand(self):
        self.assert_single_literal('count_q==(limit_q-1)',
                                   'count_q==(limit_q-1)')

    def test_a_call_used_as_an_index_is_one_operand(self):
        self.assert_single_literal('mask_q[func(idx_i)]',
                                   'mask_q[func(idx_i)]')

    def test_a_package_scoped_call_index_is_one_operand(self):
        self.assert_single_literal('opgrp_q[pkg::get_group(op_i)]',
                                   'opgrp_q[pkg::get_group(op_i)]')

    def test_a_hierarchical_reference_is_one_operand(self):
        self.assertEqual(
            clause_set(clauses_of('div_unit.state_q==IDLE->!div_unit.busy_q')),
            clause_set([['!div_unit.state_q==IDLE', '!div_unit.busy_q']]))

    def test_no_placeholder_text_reaches_any_clause(self):
        for assertion in REALISTIC_ASSERTIONS:
            with self.subTest(assertion=assertion):
                _n, _d, clauses, post = SVAParser().parse_sva(assertion)
                self.assertIsNotNone(clauses)
                for literal in literals_of(clauses) + [str(t) for t in post]:
                    self.assertNotIn('__TERM_', literal)

    def test_every_literal_is_balanced_text(self):
        for assertion in REALISTIC_ASSERTIONS:
            with self.subTest(assertion=assertion):
                clauses = SVAParser().parse_sva(assertion)[2]
                for literal in literals_of(clauses):
                    body = atom_of(literal)
                    self.assertEqual(body.count('('), body.count(')'),
                                     f'unbalanced literal {literal!r}')
                    self.assertEqual(body.count('['), body.count(']'),
                                     f'unbalanced literal {literal!r}')

    def test_every_literal_is_a_plain_string(self):
        # A stringified expression tree would satisfy the balance check while
        # being meaningless, and it compares equal to nothing but itself.
        for assertion in REALISTIC_ASSERTIONS:
            with self.subTest(assertion=assertion):
                clauses = SVAParser().parse_sva(assertion)[2]
                for clause in clauses:
                    for literal in clause:
                        self.assertIsInstance(literal, str)
                        self.assertNotIn("['", literal)
                        self.assertNotIn('[[', literal)

    def test_a_conditional_does_not_split_a_slice_or_a_scope(self):
        # ':' separates the arms of a conditional, and also indexes a range and
        # qualifies a scope.  Only the first meaning is a token.
        clauses = clauses_of(
            'ready_i ? data_q[31:0] == data_i : state_q == pkg::IDLE')
        for literal in literals_of(clauses):
            self.assertNotIn(':(', literal)
        self.assertIn('data_q[31:0]==data_i',
                      [atom_of(l) for l in literals_of(clauses)])
        self.assertIn('state_q==pkg::IDLE',
                      [atom_of(l) for l in literals_of(clauses)])

    def test_a_comparison_against_a_negated_call_is_one_operand(self):
        clauses = clauses_of('ready_o == !$past(pending_q) -> ack_o')
        self.assertEqual(clause_set(clauses),
                         clause_set([['!ready_o==!$past(pending_q)', 'ack_o']]))

    def test_a_reduction_over_a_group_is_one_operand(self):
        clauses = clauses_of('hit_i -> !(|(replace_en & mask_i))')
        self.assertEqual(clause_set(clauses),
                         clause_set([['!hit_i', '!|(replace_en&mask_i)']]))

    def test_a_reduction_operand_keeps_its_bitwise_operator(self):
        # Inside a reduction the '&' joins two vectors. Rewriting it as '&&'
        # would give two different properties the same operand text.
        body = SVAParser()._extract_structure(
            'a_x: assert property (hit_i |-> |(replace_en & mask_i));')[2]
        self.assertIn('|(replace_en&mask_i)', body)

    def test_a_group_after_a_connective_is_still_structure(self):
        # '&&(' and '||(' end in the same characters a reduction begins with;
        # reading the group as a reduction operand loses the whole disjunction.
        clauses = clauses_of('a & (b | c) -> d')
        self.assertEqual(clause_set(clauses),
                         clause_set([['!a', '!b', 'd'], ['!a', '!c', 'd']]))

    def test_no_realistic_assertion_collapses_to_a_single_operand(self):
        # 'req_i |=> gnt_o' losing its consequent would leave one literal that
        # every other property over req_i also produces.
        for assertion in REALISTIC_ASSERTIONS:
            if '|->' not in assertion and '|=>' not in assertion:
                continue
            with self.subTest(assertion=assertion):
                clauses = SVAParser().parse_sva(assertion)[2]
                self.assertGreaterEqual(len(literals_of(clauses)), 2,
                                        f'{assertion} lost an operand')


# ─────────────────────────────────────────────────────────────────────
#  The shapes the flow actually writes to the property file
# ─────────────────────────────────────────────────────────────────────

class TestGeneratedAssertionShapes(unittest.TestCase):
    """Assertions as they appear in generated property files.

    The template wraps the body in parentheses and appends a failure action, and
    models write '&' and '~' for one-bit control signals.  Each of those hid the
    property's structure, so a whole file of assertions reduced to one opaque
    operand apiece and compared equal to each other.
    """

    #: Taken verbatim from checked-in property files, whitespace collapsed.
    GENERATED = [
        'a_data_request_granted: assert property ((ptw.req_port_i.data_gnt |-> '
        "ptw.tag_valid_n == 1'b1 && ptw.state_d == ptw.PTE_LOOKUP)) "
        'else begin $display("Assertion a_data_request_granted failed"); end',
        'a_instruction_tlb_miss: assert property ((enable_translation_i & '
        'itlb_access_i & ~itlb_hit_i & ~dtlb_access_i |-> '
        'ptw.is_instr_ptw_n == 1\'b1 && ptw.state_d == ptw.WAIT_GRANT)) '
        'else begin $display("Assertion a_instruction_tlb_miss failed"); end',
        'a_state_register_updates: assert property ((disable iff(!rstn_i) '
        "(1'b1 |=> ptw_arb.current_state_q == $past(ptw_arb.next_state_d)))) "
        'else begin $display("Assertion a_state_register_updates failed"); end',
        'a_zero_inputs: assert property (remanent_i == 0 && divisor_i != 0 |-> '
        '(div_4bits.quotient_bit[1] == 0 && div_4bits.quotient_bit[0] == 0)) '
        'else begin $display("Assertion a_zero_inputs failed"); end',
        'a_in_ready_matches_format: assert property (in_ready_o == '
        '(in_valid_i && block.fmt_in_ready[block.dst_fmt_i]));',
        'a_quotient_bit_matches: assert property ((sew_i == SEW_8 |-> '
        '(div.tmp_remanent[0] >= divisor_i) == div.quotient_bit[0])) '
        'else begin $display("Assertion a_quotient_bit_matches failed"); end',
    ]

    def parse_generated(self, assertion: str):
        return SVAParser().parse_sva(re.sub(r'\s+', '', assertion))

    def test_every_generated_assertion_is_converted(self):
        for assertion in self.GENERATED:
            with self.subTest(assertion=assertion[:60]):
                clauses = self.parse_generated(assertion)[2]
                self.assertIsNotNone(clauses,
                                     'the generated form was refused')

    def test_no_generated_assertion_collapses_to_one_operand(self):
        for assertion in self.GENERATED:
            if '|->' not in assertion and '|=>' not in assertion:
                continue
            with self.subTest(assertion=assertion[:60]):
                clauses = self.parse_generated(assertion)[2]
                self.assertGreaterEqual(len(literals_of(clauses)), 2)

    def test_the_failure_action_does_not_change_the_property(self):
        bare = 'a_x: assert property (req_i |=> gnt_o);'
        with_action = ('a_x: assert property (req_i |=> gnt_o) '
                       'else begin $display("Assertion a_x failed"); end')
        self.assertEqual(SVAParser().parse_sva(bare),
                         SVAParser().parse_sva(with_action))

    def test_a_redundantly_wrapped_body_is_the_same_property(self):
        self.assertEqual(
            SVAParser().parse_sva('a_x: assert property (req_i |=> gnt_o);'),
            SVAParser().parse_sva('a_x: assert property ((req_i |=> gnt_o));'))

    def test_a_reset_guard_inside_the_wrapping_parentheses_is_recognised(self):
        # The guard used to leave the implication inside a group, where it is
        # not structural, and the property became one opaque operand.
        clauses = self.parse_generated(
            'a_x: assert property ((disable iff(!rstn_i) (req_i |=> gnt_o)));')[2]
        self.assertEqual(clause_set(clauses),
                         clause_set([['!req_i', 'gnt_o']]))

    def test_a_reset_guard_keeps_its_delay(self):
        self.assertEqual(self.parse_generated(
            'a_x: assert property ((disable iff(!rstn_i) (req_i |=> gnt_o)));')[1],
            1)

    def test_a_bitwise_and_between_control_signals_unrolls(self):
        self.assertEqual(clause_set(clauses_of('a_i & b_i -> c_o')),
                         clause_set(clauses_of('a_i && b_i -> c_o')))

    def test_a_bitwise_or_between_control_signals_unrolls(self):
        self.assertEqual(clause_set(clauses_of('a_i | b_i -> c_o')),
                         clause_set(clauses_of('a_i || b_i -> c_o')))

    def test_a_bitwise_negation_reads_as_a_negation(self):
        self.assertEqual(clause_set(clauses_of('~a_i -> b_o')),
                         clause_set(clauses_of('!a_i -> b_o')))

    def test_a_reduction_operator_is_not_read_as_a_connective(self):
        self.assertEqual(clause_set(clauses_of('&mask_i -> ok_o')),
                         clause_set([['!&mask_i', 'ok_o']]))

    def test_a_reduction_after_a_connective_stays_an_operand(self):
        self.assertEqual(clause_set(clauses_of('req_i && |mask_i -> ok_o')),
                         clause_set([['!req_i', '!|mask_i', 'ok_o']]))

    def test_a_nand_reduction_stays_one_operand(self):
        self.assert_reduction('~&mask_i -> ok_o', '~&mask_i')

    def test_a_nor_reduction_stays_one_operand(self):
        self.assert_reduction('~|mask_i -> ok_o', '~|mask_i')

    def assert_reduction(self, body: str, operand: str):
        self.assertEqual(clause_set(clauses_of(body)),
                         clause_set([['!' + operand, 'ok_o']]))


# ─────────────────────────────────────────────────────────────────────
#  Refusal instead of approximation
# ─────────────────────────────────────────────────────────────────────

class TestUnrepresentableInputIsRefused(unittest.TestCase):
    """What cannot be converted faithfully is reported, not approximated."""

    def test_an_unclosed_parenthesis_is_refused(self):
        self.assertIsNone(clauses_of('(req_i && gnt_o -> ack_o'))

    def test_an_unopened_parenthesis_is_refused(self):
        self.assertIsNone(clauses_of('req_i && gnt_o) -> ack_o'))

    def test_a_dangling_operator_is_refused(self):
        self.assertIsNone(clauses_of('req_i &&'))

    def test_two_unconnected_operands_are_refused(self):
        self.assertIsNone(SVAParser().convert_to_cnf(['a', 'b'])[0])

    def test_an_empty_body_is_refused(self):
        self.assertIsNone(SVAParser().convert_to_cnf([])[0])

    def test_a_distribution_that_would_explode_is_refused(self):
        body = '||'.join(f'(a{i}&&b{i})' for i in range(12))
        self.assertIsNone(clauses_of(body))

    def test_a_distribution_within_budget_is_still_expanded(self):
        body = '||'.join(f'(a{i}&&b{i})' for i in range(4))
        clauses = clauses_of(body)
        self.assertEqual(len(clauses), 2 ** 4)

    def test_a_refused_property_reports_no_postcondition_either(self):
        _name, _delay, clauses, post = parse('(req_i && gnt_o -> ack_o')
        self.assertIsNone(clauses)
        self.assertIsNone(post)

    def test_a_conditional_with_an_empty_arm_is_refused(self):
        self.assertIsNone(clauses_of('sel_i ? : b_o'))

    def test_a_tree_node_that_is_not_an_atom_is_refused(self):
        self.assertIsNone(SVAParser().flatten_to_cnf(['AND', ['MYSTERY', 'a']]))

    def test_text_after_the_property_is_not_mistaken_for_a_failure_action(self):
        # Here the parentheses do not close where they appear to.  Treating the
        # remainder as an action block would drop '-> ack_o' and store a
        # different property than the one written.
        self.assertIsNone(
            SVAParser().parse_sva(
                'a_x: assert property (req_i && gnt_o) -> ack_o);')[2])

    def test_the_same_truncation_is_refused_without_a_label(self):
        self.assertIsNone(
            SVAParser().parse_sva(
                'assert property (req_i && gnt_o) -> ack_o);')[2])

    def test_no_input_makes_the_parser_raise(self):
        # Every caller treats an exception as a defective assertion, so raising
        # costs a repair attempt and a quarantine for something the parser
        # should simply refuse.
        for text in ('', 'assert property (', 'assert property ();',
                     'a_x: assert property', 'always_comb x = y;',
                     'assert property (req_i |=> gnt_o'):
            with self.subTest(text=text):
                self.assertIsNone(SVAParser().parse_sva(text)[2])


# ─────────────────────────────────────────────────────────────────────
#  The property database
# ─────────────────────────────────────────────────────────────────────

class TestPropertyDatabase(unittest.TestCase):
    """Storing a property fills every dictionary the flow reads back."""

    def setUp(self):
        self.parser = SVAParser()

    def store(self, assertion: str) -> int:
        return self.parser.process_property(re.sub(r'\s+', '', assertion))

    def test_a_new_property_is_stored_once_under_its_label(self):
        self.assertEqual(self.store('a_req: assert property (req_i |=> gnt_o);'), 1)
        self.assertEqual(list(self.parser.clauses_d), ['a_req'])

    def test_every_dictionary_receives_the_property(self):
        self.store('a_req: assert property (req_i |=> gnt_o);')
        for dictionary in (self.parser.sva_string, self.parser.delay_d,
                           self.parser.clauses_d, self.parser.postcondition_d):
            self.assertIn('a_req', dictionary)

    def test_the_stored_delay_matches_the_assertion(self):
        self.store('a_req: assert property (req_i |-> ##3 gnt_o);')
        self.assertEqual(self.parser.delay_d['a_req'], 3)

    def test_the_stored_clauses_match_the_assertion(self):
        self.store('a_req: assert property (req_i |=> gnt_o);')
        self.assertEqual(clause_set(self.parser.clauses_d['a_req']),
                         clause_set([['!req_i', 'gnt_o']]))

    def test_a_reused_label_is_renamed_and_both_are_kept(self):
        self.store('a_req: assert property (req_i |=> gnt_o);')
        self.assertEqual(self.store('a_req: assert property (ack_i |=> done_o);'), 1)
        self.assertEqual(len(self.parser.clauses_d), 2)
        self.assertIn('a_req_0', self.parser.clauses_d)

    def test_an_assertion_without_a_label_is_stored_under_a_placeholder(self):
        # Unlabelled is legal SVA. It used to be reported as 'not new', which
        # the caller reads as a duplicate, so it counted towards saturation.
        self.assertEqual(self.store('assert property (req_i |=> gnt_o);'), 1)
        self.assertEqual(clause_set(self.parser.clauses_d['unnamed_property']),
                         clause_set([['!req_i', 'gnt_o']]))

    def test_a_second_unlabelled_copy_is_a_duplicate(self):
        self.store('assert property (req_i |=> gnt_o);')
        self.assertEqual(self.store('assert property (req_i |=> gnt_o);'), 0)
        self.assertEqual(len(self.parser.clauses_d), 1)

    def test_text_that_is_not_an_assertion_is_not_stored(self):
        self.assertEqual(self.store('always_comb x = y;'), 0)
        self.assertEqual(self.parser.clauses_d, {})

    def test_an_unrepresentable_property_is_recorded_separately(self):
        self.assertEqual(
            self.store('a_bad: assert property ((req_i && gnt_o -> ack_o);'), 1)
        self.assertIn('a_bad', self.parser.unprocessable_props_dict)
        self.assertNotIn('a_bad', self.parser.clauses_d)

    def test_the_database_validates_after_a_mixed_batch(self):
        self.store('a_one: assert property (req_i |=> gnt_o);')
        self.store('a_two: assert property (ack_i |-> ##2 done_o);')
        self.store('a_bad: assert property ((req_i && gnt_o -> ack_o);')
        self.assertTrue(self.parser.validate_no_duplicates())


# ─────────────────────────────────────────────────────────────────────
#  Duplicate detection, which is the stop condition
# ─────────────────────────────────────────────────────────────────────

class TestDuplicateDetection(unittest.TestCase):
    """The extension loop stops when a batch adds nothing, so a wrong
    duplicate verdict ends generation early and silently."""

    def setUp(self):
        self.parser = SVAParser()

    def store(self, assertion: str) -> int:
        return self.parser.process_property(re.sub(r'\s+', '', assertion))

    def test_the_same_property_twice_adds_one(self):
        self.assertEqual(self.store('a_one: assert property (req_i |=> gnt_o);'), 1)
        self.assertEqual(self.store('a_two: assert property (req_i |=> gnt_o);'), 0)

    def test_reordered_conjuncts_are_the_same_property(self):
        self.store('a_one: assert property (req_i && ack_i |=> gnt_o);')
        self.assertEqual(
            self.store('a_two: assert property (ack_i && req_i |=> gnt_o);'), 0)

    def test_a_different_grouping_is_a_different_property(self):
        self.store('a_one: assert property ((a_i || b_i) && c_i |=> d_o);')
        self.assertEqual(
            self.store('a_two: assert property (a_i || (b_i && c_i) |=> d_o);'), 1)

    def test_a_different_delay_is_a_different_property(self):
        self.store('a_one: assert property (req_i |-> gnt_o);')
        self.assertEqual(
            self.store('a_two: assert property (req_i |=> gnt_o);'), 1)

    def test_the_same_delay_in_a_different_place_is_a_different_property(self):
        self.store('a_one: assert property (req_i |-> ##2 gnt_o);')
        self.assertEqual(
            self.store('a_two: assert property (req_i ##2 ack_i |-> gnt_o);'), 1)

    def test_swapped_signals_are_a_different_property(self):
        self.store('a_one: assert property (x_i |=> y_o);')
        self.assertEqual(self.store('a_two: assert property (y_i |=> x_o);'), 1)

    def test_a_different_consequent_is_a_different_property(self):
        self.store('a_one: assert property (req_i |=> gnt_o);')
        self.assertEqual(
            self.store('a_two: assert property (req_i |=> !gnt_o);'), 1)

    def test_a_relabelled_unrepresentable_property_is_the_same_property(self):
        # Comparing names alone let the same unparsable property come back
        # under a new label and keep the extension loop running.
        self.assertEqual(
            self.store('a_bad: assert property ((req_i && gnt_o -> ack_o);'), 1)
        self.assertEqual(
            self.store('a_bad_again: assert property ((req_i && gnt_o -> ack_o);'), 0)

    def test_a_different_unrepresentable_property_is_still_new(self):
        self.store('a_bad: assert property ((req_i && gnt_o -> ack_o);')
        self.assertEqual(
            self.store('a_other: assert property ((ack_i && done_o -> err_o);'), 1)

    def test_checking_for_a_duplicate_does_not_store_it(self):
        assertion = re.sub(r'\s+', '', 'a_one: assert property (req_i |=> gnt_o);')
        self.assertFalse(self.parser.check_property_duplicate(assertion))
        self.assertEqual(self.parser.clauses_d, {})
        self.assertFalse(self.parser.check_property_duplicate(assertion))

    def test_checking_agrees_with_storing(self):
        self.store('a_one: assert property (req_i |=> gnt_o);')
        repeat = re.sub(r'\s+', '', 'a_two: assert property (req_i |=> gnt_o);')
        novel = re.sub(r'\s+', '', 'a_three: assert property (ack_i |=> done_o);')
        self.assertTrue(self.parser.check_property_duplicate(repeat))
        self.assertFalse(self.parser.check_property_duplicate(novel))

    def test_a_batch_of_repeats_adds_nothing_and_a_novel_one_adds_one(self):
        for assertion in ('a_one: assert property (req_i |=> gnt_o);',
                          'a_two: assert property (ack_i |-> done_o);'):
            self.store(assertion)
        repeats = ['a_three: assert property (req_i |=> gnt_o);',
                   'a_four: assert property (ack_i |-> done_o);']
        self.assertEqual(sum(self.store(a) for a in repeats), 0)
        self.assertEqual(
            self.store('a_five: assert property (busy_q |=> !idle_o);'), 1)


# ─────────────────────────────────────────────────────────────────────
#  The gate the agent puts in front of the proof stage
# ─────────────────────────────────────────────────────────────────────

class TestAgentQuarantineGate(unittest.TestCase):
    """The agent comments out properties whose CNF is not trustworthy."""

    def test_a_missing_clause_list_is_flagged(self):
        self.assertTrue(agent_module._cnf_imperfect(None, None))

    def test_placeholder_text_in_a_literal_is_flagged(self):
        self.assertTrue(agent_module._cnf_imperfect([['__TERM_0__', 'b']]))

    def test_placeholder_text_in_the_postcondition_is_flagged(self):
        self.assertTrue(agent_module._cnf_imperfect([['a']], ['__TERM_1__']))

    def test_an_unbalanced_literal_is_flagged(self):
        self.assertTrue(agent_module._cnf_imperfect([['!(a==b', 'c']]))

    def test_a_stringified_expression_tree_is_flagged(self):
        self.assertTrue(
            agent_module._cnf_imperfect([["![['OR', 'a', 'b']]", 'c']]))

    def test_a_clean_clause_list_passes(self):
        self.assertFalse(
            agent_module._cnf_imperfect([['!req_i', 'gnt_o']], ['##1', 'gnt_o']))

    def test_every_realistic_assertion_passes_the_gate(self):
        for assertion in REALISTIC_ASSERTIONS:
            with self.subTest(assertion=assertion):
                _n, _d, clauses, post = SVAParser().parse_sva(assertion)
                self.assertFalse(agent_module._cnf_imperfect(clauses, post),
                                 f'{assertion} would be quarantined')


# ─────────────────────────────────────────────────────────────────────
#  Agreement between the two extractors
# ─────────────────────────────────────────────────────────────────────

class TestExtractorAgreement(unittest.TestCase):
    """The grammar and the regex fallback must produce the same tuple.

    Both paths are live in one session, so a disagreement would make the same
    property look new depending on which extractor happened to accept it.
    """

    def test_the_grammar_backed_extractor_is_the_one_in_use(self):
        # The runtime is a declared dependency; without it every property
        # silently takes the fallback path.
        self.assertTrue(svaparser._ANTLR_AVAILABLE,
                        'the ANTLR runtime is not importable')

    def test_both_extractors_agree_on_the_sample_assertions(self):
        for assertion in REALISTIC_ASSERTIONS:
            normalised = re.sub(r'\s+', '', assertion)
            with self.subTest(assertion=assertion):
                grammar = SVAParser()._extract_structure(normalised)
                legacy = SVAParser()._legacy_preprocess(normalised)
                self.assertEqual(grammar, legacy)

    def test_both_extractors_agree_on_an_unlabelled_property(self):
        parser = SVAParser()
        text = re.sub(r'\s+', '',
                      'assert property (req_i && ack_i |=> gnt_o);')
        labelled, added = parser._with_label(parser._trim_to_property(text))
        self.assertTrue(added)
        grammar = parser._extract_structure(text)
        legacy = parser._legacy_preprocess(labelled)
        # Delay, body and consequent delay must match; the placeholder label is
        # an artefact of the grammar and must not be reported as a name.
        self.assertEqual(grammar[1:], legacy[1:])
        self.assertEqual(grammar[0], '')


# ─────────────────────────────────────────────────────────────────────
#  Limits that are accepted on purpose
# ─────────────────────────────────────────────────────────────────────

class TestAcceptedLimits(unittest.TestCase):
    """Operands the converter treats as opaque, and what that costs.

    An opaque operand keeps the boolean skeleton exact and gives up equalities
    between operands. The cost is a property counted as new when an equivalent
    one is already held: budget, never coverage. These tests pin that direction
    down, because the opposite error ends the loop early.
    """

    def store_pair(self, first: str, second: str) -> int:
        parser = SVAParser()
        parser.process_property(re.sub(r'\s+', '', first))
        return parser.process_property(re.sub(r'\s+', '', second))

    def test_a_commuted_comparison_is_not_recognised(self):
        self.assertEqual(
            self.store_pair('a_one: assert property (a_i == b_i |=> c_o);',
                            'a_two: assert property (b_i == a_i |=> c_o);'), 1)

    def test_a_negated_comparison_is_not_recognised(self):
        self.assertEqual(
            self.store_pair('a_one: assert property (!(a_i == b_i) |=> c_o);',
                            'a_two: assert property (a_i != b_i |=> c_o);'), 1)

    def test_a_temporal_operator_becomes_one_opaque_operand(self):
        clauses = clauses_of('req_i throughout gnt_o -> ack_o')
        self.assertEqual(len(clauses), 1)
        self.assertEqual(len(clauses[0]), 2)

    def test_opaque_operands_still_tell_different_properties_apart(self):
        self.assertEqual(
            self.store_pair(
                'a_one: assert property ((s_q inside {IDLE, RUN}) |-> !e_o);',
                'a_two: assert property ((s_q inside {IDLE, WAIT}) |-> !e_o);'),
            1)

    def test_a_question_mark_without_an_else_arm_stays_one_operand(self):
        # Characterisation: a '?' with no arm to match is not a conditional, and
        # it is also how a don't-care literal is written, so it is left inside
        # the operand rather than guessed at.  Lint rejects the shape upstream.
        self.assertEqual(clauses_of('sel_i ? a_o'), [['sel_i?a_o']])

    def test_an_equivalence_binds_tighter_than_the_property_implication(self):
        # SystemVerilog gives '->' and '<->' one precedence level, so
        # 'a <-> b -> c' is 'a <-> (b -> c)'.  Here the trailing '->' is what
        # the extractor left of '|->', which is looser than any boolean
        # operator, so it is read as '(a <-> b) -> c'.  A written-out boolean
        # '->' after a '<->' is the one shape that differs from the standard.
        self.assertEqual(clause_set(clauses_of('a<->b->c')),
                         clause_set(clauses_of('(a<->b)->c')))

    def test_a_delay_range_keeps_the_property_distinguishable(self):
        # The grammar does not cover ##[m:n]; the fallback keeps the range as
        # operand text, which is enough to keep two ranges apart.
        self.assertEqual(
            self.store_pair('a_one: assert property (req_i |-> ##[1:3] gnt_o);',
                            'a_two: assert property (req_i |-> ##[2:4] gnt_o);'),
            1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
