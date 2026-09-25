#!/usr/bin/env python3
"""The parser against every assertion SVApshot has written into a property file.

The rest of the parser suite is a sample set chosen to cover the functionality,
with every input a literal in the test file.  This suite is the other direction:
real assertions from real runs, kept as collateral in ``tests/collateral``, used
to check that the sample set is representative and that no shape in the corpus is
mishandled.

A slice runs by default, spread across the whole collection, so the collateral is
exercised on every run without slowing the suite down.  The full pass takes about
forty seconds and is what CI / ``scripts/ci/sanity.sh`` always run
(``SVAPSHOT_FULL_CORPUS=1``, and ``CI=true`` or ``SVAPSHOT_REQUIRE_FULL_CORPUS=1``
refuse to fall back to the slice):

    SVAPSHOT_FULL_CORPUS=1 python3 tests/test_svaparser_corpus.py

The invariants are the failure modes that a clause list must never show, because
each of them makes two properties compare wrongly and the extension loop stop for
the wrong reason.  The refusal count is a bound rather than an equality: fewer
refusals is an improvement, more is a regression.
"""

from __future__ import annotations

import os
import re
import sys
import unittest

import _path_setup  # noqa: F401, E402

import agent as agent_module
from svaparser import SVAParser

CORPUS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      'collateral', 'assertion_corpus.sv')

#: Set SVAPSHOT_FULL_CORPUS=1 to parse every assertion instead of a slice.
#: GitLab sets CI=true; sanity stage 1 also exports SVAPSHOT_REQUIRE_FULL_CORPUS=1
#: so a missing env var cannot silently run the 60-assertion slice in CI.
_IN_CI = os.environ.get('CI', '').lower() in {'1', 'true', 'yes'}
_REQUIRE_FULL_CORPUS = (
    os.environ.get('SVAPSHOT_REQUIRE_FULL_CORPUS') == '1' or _IN_CI)
FULL_CORPUS = os.environ.get('SVAPSHOT_FULL_CORPUS') == '1' or _REQUIRE_FULL_CORPUS

#: Assertions in the default slice, and in the smaller one used for the tests
#: that store properties, where every comparison is against everything already
#: held.  The grammar costs about five milliseconds per assertion.
SLICE_SIZE = 60
DATABASE_SLICE_SIZE = 25

#: Refusals over the whole collection, recorded when the collateral was written.
#: They are four overlapping families: a comparison whose other side is arithmetic
#: over concatenations, replications or part-selects, a conditional inside one of
#: those operands, a multi-cycle sequence or a second implication in the
#: consequent, and a reduction applied to a negated operand.
KNOWN_REFUSALS = 34


def load_corpus():
    if not os.path.exists(CORPUS):
        return None
    with open(CORPUS, errors='replace') as handle:
        return [line.strip() for line in handle
                if line.strip() and not line.lstrip().startswith('//')]


def parser_input(assertion: str) -> str:
    """The property alone, whitespace free, exactly as the agent hands it over."""
    return agent_module.CodingAgent._parser_input(assertion)


#: Parsed once for the whole module: the grammar is what the suite spends its
#: time on, and every check below reads the same tuples.
_CACHE = {}


def parse_sample():
    if _CACHE:
        return _CACHE
    corpus = load_corpus()
    if corpus is None:
        _CACHE['corpus'] = None
        return _CACHE
    step = max(1, len(corpus) // SLICE_SIZE)
    sample = corpus if FULL_CORPUS else corpus[::step]

    parser = SVAParser()
    parsed, raised = [], []
    for assertion in sample:
        try:
            _name, delay, clauses, post = parser.parse_sva(parser_input(assertion))
        except Exception as exc:                     # noqa: BLE001 - reported below
            raised.append((assertion, f'{type(exc).__name__}: {exc}'))
            continue
        parsed.append((assertion, delay, clauses, post))

    _CACHE.update(corpus=corpus, sample=sample, parsed=parsed, raised=raised,
                  database_sample=sample[:DATABASE_SLICE_SIZE])
    return _CACHE


class CorpusTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cache = parse_sample()
        if cache['corpus'] is None:
            raise unittest.SkipTest(
                f'assertion corpus collateral is missing: {CORPUS}')
        cls.corpus = cache['corpus']
        cls.sample = cache['sample']
        cls.parsed = cache['parsed']
        cls.raised = cache['raised']
        cls.database_sample = cache['database_sample']
        mode = 'full' if FULL_CORPUS else 'slice'
        print(
            f'assertion corpus: {len(cls.sample)}/{len(cls.corpus)} ({mode})',
            file=sys.stderr)

    @property
    def accepted(self):
        """(assertion, delay, clauses, postcondition) for the ones converted."""
        return [entry for entry in self.parsed
                if entry[2] is not None and entry[3] is not None]


class TestTheCorpusIsConverted(CorpusTestCase):
    """No assertion raises, collapses, leaks a placeholder or fails the gate."""

    def test_no_assertion_makes_the_parser_raise(self):
        self.assertEqual(self.raised, [])

    def test_no_literal_holds_a_placeholder_or_a_tree(self):
        for assertion, _delay, clauses, _post in self.accepted:
            with self.subTest(assertion=assertion[:70]):
                for clause in clauses:
                    for literal in clause:
                        self.assertIsInstance(literal, str)
                        self.assertNotIn('__TERM_', literal)
                        self.assertNotIn("['", literal)
                        self.assertNotIn('[[', literal)

    def test_every_literal_is_balanced(self):
        for assertion, _delay, clauses, _post in self.accepted:
            with self.subTest(assertion=assertion[:70]):
                for clause in clauses:
                    for literal in clause:
                        self.assertTrue(SVAParser._is_balanced(literal),
                                        f'unbalanced literal {literal!r}')

    def test_no_accepted_property_would_be_quarantined(self):
        for assertion, _delay, clauses, post in self.accepted:
            with self.subTest(assertion=assertion[:70]):
                self.assertFalse(agent_module._cnf_imperfect(clauses, post))

    def test_an_implication_keeps_both_sides(self):
        # One literal for a property that has an implication means the operands
        # were lost, and every property over that operand then collides with it.
        for assertion, _delay, clauses, _post in self.accepted:
            if '|->' not in assertion and '|=>' not in assertion:
                continue
            literals = [l for clause in clauses for l in clause]
            if len(literals) == 1:
                # 'a |-> !a' really is one literal; nothing else may be.
                self.assertRegex(re.sub(r'\s+', '', assertion),
                                 r'\(\s*(!*)(\w[\w.$\[\]]*)\s*\|[-=]>\s*!+\2',
                                 f'{assertion} lost an operand')

    def test_full_corpus_mode_does_not_slice(self):
        if not FULL_CORPUS:
            self.skipTest('run with SVAPSHOT_FULL_CORPUS=1')
        self.assertEqual(
            len(self.sample), len(self.corpus),
            'SVAPSHOT_FULL_CORPUS=1 must parse every stored assertion, not a slice')

    def test_refusals_stay_within_the_recorded_bound(self):
        if not FULL_CORPUS:
            self.skipTest('run with SVAPSHOT_FULL_CORPUS=1')
        parser = SVAParser()
        refused = [assertion for assertion in self.corpus
                   if parser.parse_sva(parser_input(assertion))[2] is None]
        self.assertLessEqual(
            len(refused), KNOWN_REFUSALS,
            'more assertions are refused than when the corpus was recorded:\n'
            + '\n'.join(refused[:10]))


class TestIdentityIsStableAcrossTheCorpus(CorpusTestCase):
    """The tuple is a property's identity, so it must not depend on the run."""

    def test_parsing_twice_gives_the_same_tuple(self):
        second = SVAParser()
        for assertion, delay, clauses, post in self.parsed:
            with self.subTest(assertion=assertion[:70]):
                self.assertEqual(second.parse_sva(parser_input(assertion))[1:],
                                 (delay, clauses, post))

    def test_the_same_batch_twice_adds_nothing_the_second_time(self):
        # This is the stop condition: a batch that repeats what the database
        # already holds must add no property, whatever order it arrives in.
        parser = SVAParser()
        added = sum(parser.process_property(parser_input(assertion))
                    for assertion in self.database_sample)
        again = sum(parser.process_property(parser_input(assertion))
                    for assertion in reversed(self.database_sample))
        self.assertGreater(added, 0)
        self.assertEqual(again, 0)

    def test_relabelling_every_assertion_changes_nothing(self):
        parser = SVAParser()
        for assertion in self.database_sample:
            parser.process_property(parser_input(assertion))
        stored = len(parser.clauses_d) + len(parser.unprocessable_props_dict)

        relabelled = SVAParser()
        for index, assertion in enumerate(self.database_sample):
            text = re.sub(r'^\s*\w+\s*:', f'renamed_{index}:', assertion)
            relabelled.process_property(parser_input(text))
        self.assertEqual(
            len(relabelled.clauses_d) + len(relabelled.unprocessable_props_dict),
            stored)


if __name__ == '__main__':
    unittest.main(verbosity=2)
