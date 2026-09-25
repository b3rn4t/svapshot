#!/usr/bin/env python3
"""Unit tests for the property-set extension loop in ``agent.py``.

Extension is where coverage comes from: the model is asked for more assertions,
and the loop keeps asking until a batch adds none.  Two things therefore have to
be right, and they pull against each other.

* **Stopping too late** saturates the property file with restatements of what is
  already there.  Every one of them costs a formal run, and none of them can
  catch a bug the existing set does not.
* **Stopping too early** ends generation while coverage is still reachable, and
  nothing downstream can tell that it happened.

Both are decided by one question asked once per assertion: is this property
already in the database?  The database itself is covered by
``test_svaparser.py``; what is covered here is the loop built on top of it —
which assertions are offered to it, which are accumulated, what the model is
asked, and what ends up in the property file.

The model is scripted, so no LLM is involved and the loop runs in milliseconds.
``CodingAgent`` is instantiated without its constructor, and only the state the
loop reads is provided.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import tempfile
import unittest

import _path_setup  # noqa: F401, E402

import agent as agent_module
import svaparser
from assertion_template import assemble_assertion, get_assertion_name

CORPUS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      'collateral', 'assertion_corpus.sv')

RTL = """\
module tiny (
    input  logic clk_i,
    input  logic rst_ni,
    input  logic req_i,
    input  logic ack_i,
    input  logic valid_i,
    output logic gnt_o,
    output logic done_o
);
    logic busy_q, busy_d;
endmodule
"""

CHECKER = 'module tiny_prop;\n'


def assertion(name: str, prop: str, failure: str | None = None) -> str:
    return assemble_assertion(name, prop, failure or f'{name} failed')


class ScriptedModel:
    """Stands in for the LLM: hands back prepared batches, and counts the asks.

    ``_add_assertions`` replaces ``self.assertions`` with what it could extract
    from the response and writes the property file.  A response it cannot read
    any assertion out of leaves ``self.assertions`` untouched, so a batch of
    ``None`` here means exactly that.
    """

    def __init__(self, batches):
        self.batches = [None if batch is None else list(batch)
                        for batch in batches]
        self.asked_with = []

    @property
    def calls(self) -> int:
        return len(self.asked_with)

    def respond(self, agent):
        self.asked_with.append(list(agent.initial_assertions))
        batch = self.batches.pop(0) if self.batches else None
        if batch is None:
            return
        agent.assertions = list(batch)
        agent._write_assertions()


class ExtensionHarness(unittest.TestCase):
    """An agent holding only what the extension loop touches."""

    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.original_cap = agent_module.MAX_ASSERTIONS
        self.addCleanup(self.restore_cap)
        # The database prints a validation report per batch; the loop runs it
        # several times per test, and none of it is what is under test here.
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def restore_cap(self):
        agent_module.MAX_ASSERTIONS = self.original_cap

    def cap(self, value):
        agent_module.MAX_ASSERTIONS = value

    def make_agent(self, initial, batches=()):
        agent = agent_module.CodingAgent.__new__(agent_module.CodingAgent)
        agent.rtl = RTL
        agent.rtl_module = 'tiny.sv'
        agent.instantiation_context = ''
        agent.verbosity = 0
        agent._log = lambda *args, **kwargs: None
        agent.parser = svaparser.SVAParser()
        agent.metrics = {'extension_iterations': 0,
                         'internal_port_binds': 0,
                         'duplicate_name_fixes': 0}
        agent.assertions = list(initial)
        agent.assertions_dir = self.workdir.name + '/'
        agent.assertions_file = 'tiny_prop.sv'
        agent.checker_code = CHECKER
        agent.initial_syntax_iterations = 0
        agent.syntax_iterations = 0
        agent._get_highest_file_index = lambda: 0

        model = ScriptedModel(batches)
        agent._add_assertions = lambda: model.respond(agent)
        self.corrected = []
        agent._correct_syntax = lambda *args: self.corrected.append(
            list(agent.assertions))

        # The flow processes the syntax-corrected initial batch before extension
        # starts, so the database already holds it when the loop asks for more.
        agent._process_batch(limit=agent_module.MAX_ASSERTIONS)
        agent._write_assertions()

        self.agent = agent
        self.model = model
        return agent, model

    def run_extension(self, initial, batches=()):
        agent, model = self.make_agent(initial, batches)
        agent._extend_property_set()
        return agent, model

    def property_file(self) -> str:
        path = os.path.join(self.workdir.name, 'tiny_prop.sv')
        with open(path) as handle:
            return handle.read()

    def written_names(self):
        return re.findall(r'^\s*(\w+)\s*:\s*assert\s+property',
                          self.property_file(), re.MULTILINE)

    def stored_properties(self) -> int:
        return (len(self.agent.parser.clauses_d)
                + len(self.agent.parser.unprocessable_props_dict))


REQ_GNT = assertion('a_gnt', 'req_i |=> gnt_o')
VALID_DONE = assertion('a_done', 'valid_i |=> done_o')
ACK_BUSY = assertion('a_busy', 'ack_i |-> tiny.busy_q')


class TestTheLoopStopsWhenNothingIsNew(ExtensionHarness):
    """The stop condition: keep asking only while a batch adds a property."""

    def test_a_batch_of_duplicates_ends_the_loop(self):
        repeat = assertion('a_repeat', 'req_i |=> gnt_o')
        _agent, model = self.run_extension([REQ_GNT], [[repeat]])
        self.assertEqual(model.calls, 1)
        self.assertEqual(self.written_names(), ['a_gnt'])

    def test_the_same_batch_verbatim_ends_the_loop(self):
        _agent, model = self.run_extension([REQ_GNT], [[REQ_GNT]])
        self.assertEqual(model.calls, 1)
        self.assertEqual(self.corrected, [])

    def test_a_response_with_no_assertions_ends_the_loop(self):
        _agent, model = self.run_extension([REQ_GNT], [None])
        self.assertEqual(model.calls, 1)
        self.assertEqual(self.written_names(), ['a_gnt'])

    def test_the_loop_asks_again_while_batches_add_something(self):
        agent, model = self.run_extension(
            [REQ_GNT], [[VALID_DONE], [ACK_BUSY], [assertion('a_again',
                                                             'req_i |=> gnt_o')]])
        self.assertEqual(model.calls, 3)
        self.assertEqual(len(agent.assertions), 3)

    def test_each_ask_carries_the_set_accumulated_so_far(self):
        _agent, model = self.run_extension(
            [REQ_GNT], [[VALID_DONE], [ACK_BUSY], None])
        self.assertEqual([len(asked) for asked in model.asked_with], [1, 2, 3])

    def test_a_property_with_a_different_consequent_is_new(self):
        # The counterpart to every duplicate test: stopping must not be eager.
        other = assertion('a_other', 'req_i |=> done_o')
        agent, model = self.run_extension([REQ_GNT], [[other], None])
        self.assertEqual(model.calls, 2)
        self.assertEqual(sorted(self.written_names()), ['a_gnt', 'a_other'])

    def test_a_batch_that_mixes_new_and_repeated_keeps_only_the_new(self):
        batch = [assertion('a_copy', 'req_i |=> gnt_o'), VALID_DONE]
        agent, model = self.run_extension([REQ_GNT], [batch, None])
        self.assertEqual(sorted(self.written_names()), ['a_done', 'a_gnt'])


class TestTheStopConditionIgnoresSpelling(ExtensionHarness):
    """A restatement of a stored property must not read as new work.

    Each of these is one property written a second way.  The database decides
    it, but the loop is what pays for a wrong answer, so the shapes a model
    actually produces are pinned here as well.
    """

    def assertRestatementStops(self, original: str, restatement: str):
        agent, model = self.run_extension(
            [assertion('a_first', original)],
            [[assertion('a_second', restatement)]])
        self.assertEqual(self.written_names(), ['a_first'],
                         f'{restatement!r} was taken for a new property')
        self.assertEqual(model.calls, 1)

    def test_reordered_conjuncts_stop_the_loop(self):
        self.assertRestatementStops('req_i && ack_i |=> gnt_o',
                                    'ack_i && req_i |=> gnt_o')

    def test_a_relabelled_copy_stops_the_loop(self):
        self.assertRestatementStops('req_i |=> gnt_o', 'req_i |=> gnt_o')

    def test_the_delay_written_out_stops_the_loop(self):
        self.assertRestatementStops('req_i |=> gnt_o', 'req_i |-> ##1 gnt_o')

    def test_redundant_parentheses_stop_the_loop(self):
        self.assertRestatementStops('req_i && ack_i |=> gnt_o',
                                    '((req_i && ack_i)) |=> gnt_o')

    def test_a_bitwise_connective_stops_the_loop(self):
        self.assertRestatementStops('req_i && ack_i |=> gnt_o',
                                    'req_i & ack_i |=> gnt_o')

    def test_de_morgan_stops_the_loop(self):
        self.assertRestatementStops('!(req_i && ack_i) |=> gnt_o',
                                    '!req_i || !ack_i |=> gnt_o')

    def test_a_different_delay_is_a_different_property(self):
        agent, model = self.run_extension(
            [assertion('a_first', 'req_i |=> gnt_o')],
            [[assertion('a_second', 'req_i |-> ##2 gnt_o')], None])
        self.assertEqual(sorted(self.written_names()), ['a_first', 'a_second'])


class TestNothingIsSpentOnDuplicates(ExtensionHarness):
    """Filtering happens before the expensive steps, and stores nothing."""

    def test_duplicates_never_reach_syntax_correction(self):
        repeat = assertion('a_repeat', 'req_i |=> gnt_o')
        self.run_extension([REQ_GNT], [[repeat]])
        self.assertEqual(self.corrected, [])

    def test_the_filter_stores_nothing(self):
        agent, _model = self.make_agent([REQ_GNT])
        before = self.stored_properties()
        unique, duplicates = agent._check_pre_syntax_duplicates(
            [VALID_DONE, assertion('a_repeat', 'req_i |=> gnt_o')])
        self.assertEqual(len(unique), 1)
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(self.stored_properties(), before)

    def test_only_the_unique_part_of_a_batch_is_corrected(self):
        batch = [assertion('a_copy', 'req_i |=> gnt_o'), VALID_DONE]
        self.run_extension([REQ_GNT], [batch, None])
        self.assertEqual(self.corrected, [[VALID_DONE]])

    def test_the_model_is_not_asked_again_after_a_batch_adds_nothing(self):
        never_used = [VALID_DONE]
        _agent, model = self.run_extension(
            [REQ_GNT], [[assertion('a_copy', 'req_i |=> gnt_o')], never_used])
        self.assertEqual(model.calls, 1)

    def test_a_batch_repeating_itself_is_offered_once(self):
        # Two spellings of one property inside a single response are duplicates
        # of each other, not of the database, so nothing before this point can
        # have filtered them.  Correcting and proving both costs twice for one
        # property, and only one of them can ever be stored.
        batch = [assertion('a_one', 'valid_i |=> done_o'),
                 assertion('a_two', 'valid_i |=> done_o')]
        self.run_extension([REQ_GNT], [batch, None])
        self.assertEqual(self.corrected, [[batch[0]]])


class TestWhatTheLoopAccumulates(ExtensionHarness):
    """The property file at the end is the accumulated set, and only that."""

    def test_the_initial_set_survives_extension(self):
        self.run_extension([REQ_GNT, VALID_DONE], [[ACK_BUSY], None])
        self.assertEqual(sorted(self.written_names()),
                         ['a_busy', 'a_done', 'a_gnt'])

    def test_duplicates_are_never_accumulated(self):
        batch = [assertion('a_copy', 'req_i |=> gnt_o'),
                 assertion('a_copy_two', 'req_i |-> ##1 gnt_o')]
        agent, _model = self.run_extension([REQ_GNT], [batch])
        self.assertEqual(agent.assertions, [REQ_GNT])

    def test_the_file_holds_every_accumulated_assertion(self):
        agent, _model = self.run_extension([REQ_GNT], [[VALID_DONE], None])
        for stored in agent.assertions:
            self.assertIn(get_assertion_name(stored), self.written_names())

    def test_the_file_and_the_database_hold_the_same_properties(self):
        # A property in the database but not in the file blocks a future
        # proposal that the file never received; the reverse leaves a property
        # in the file that no later batch is compared against.
        self.run_extension([REQ_GNT], [[VALID_DONE], [ACK_BUSY], None])
        self.assertEqual(len(self.written_names()), self.stored_properties())

    def test_the_initial_set_is_not_carried_in_twice(self):
        # The first batch is stored property by property, so a restatement
        # inside it is counted once, but every assertion was kept for the file
        # anyway: two labels for one property, both consuming a slot under the
        # cap and both proved on every run.
        twin = assertion('a_gnt_again', 'ack_i && req_i |=> gnt_o')
        agent, _model = self.run_extension(
            [REQ_GNT, assertion('a_conj', 'req_i && ack_i |=> gnt_o'), twin],
            [None])
        self.assertEqual(sorted(self.written_names()), ['a_conj', 'a_gnt'])
        self.assertEqual(self.stored_properties(), 2)

    def test_labels_stay_unique_in_the_file(self):
        clash = assertion('a_gnt', 'valid_i |=> done_o')
        self.run_extension([REQ_GNT], [[clash], None])
        names = self.written_names()
        self.assertEqual(len(names), len(set(names)))

    def test_every_round_of_asking_is_counted(self):
        # The metric existed and was never written, so every report said the
        # extension stage ran zero iterations however long it had run.
        agent, model = self.run_extension(
            [REQ_GNT], [[VALID_DONE], [ACK_BUSY], None])
        self.assertEqual(agent.metrics['extension_iterations'], model.calls)
        self.assertEqual(agent.metrics['extension_iterations'], 3)

    def test_a_stage_that_never_asks_counts_nothing(self):
        self.cap(1)
        agent, _model = self.run_extension([REQ_GNT], [[VALID_DONE]])
        self.assertEqual(agent.metrics['extension_iterations'], 0)


class TestTheAssertionCapHolds(ExtensionHarness):
    """The cap is the loop's other stop condition, and it binds the database."""

    def batch_of(self, count, start=0):
        return [assertion(f'a_extra_{index}', f'req_i ##{index + 2} |-> gnt_o')
                for index in range(start, start + count)]

    def test_the_final_set_never_exceeds_the_cap(self):
        self.cap(4)
        agent, _model = self.run_extension(
            [REQ_GNT], [self.batch_of(2), self.batch_of(4, start=2), None])
        self.assertLessEqual(len(agent.assertions), 4)
        self.assertLessEqual(len(self.written_names()), 4)

    def test_a_full_set_asks_the_model_nothing(self):
        self.cap(2)
        _agent, model = self.run_extension([REQ_GNT, VALID_DONE],
                                           [[ACK_BUSY]])
        self.assertEqual(model.calls, 0)

    def test_a_batch_beyond_the_cap_is_trimmed_to_the_slots_left(self):
        self.cap(3)
        agent, _model = self.run_extension([REQ_GNT], [self.batch_of(5), None])
        self.assertEqual(len(agent.assertions), 3)

    def test_overflow_never_reaches_syntax_correction(self):
        # The cap used to bind only after syntax and FPV, so a single LLM dump
        # of hundreds of unique properties still paid a formal run each.
        self.cap(3)
        extras = self.batch_of(5)
        self.run_extension([REQ_GNT], [extras, None])
        self.assertEqual(len(self.corrected), 1)
        self.assertEqual(self.corrected[0], extras[:2])

    def test_the_loop_stops_as_soon_as_the_cap_is_filled(self):
        self.cap(3)
        extras = self.batch_of(5)
        unused = self.batch_of(3, start=10)
        _agent, model = self.run_extension([REQ_GNT], [extras, unused])
        self.assertEqual(model.calls, 1)
        self.assertEqual(len(self.corrected), 1)

    def test_what_the_cap_discards_is_not_left_in_the_database(self):
        # A property counted, stored and then dropped for want of a slot is
        # invisible to every later stage but still answers "already got this
        # one", so the same property can never be proposed again.
        self.cap(3)
        self.run_extension([REQ_GNT], [self.batch_of(5), None])
        self.assertEqual(self.stored_properties(), len(self.written_names()))

    def test_an_initial_set_beyond_the_cap_is_trimmed_on_both_sides(self):
        self.cap(2)
        agent, _model = self.run_extension(
            [REQ_GNT, VALID_DONE, ACK_BUSY], [[assertion('a_more',
                                                         'req_i |-> ##3 done_o')]])
        self.assertEqual(len(self.written_names()), 2)
        self.assertEqual(self.stored_properties(), 2)


class TestPropertiesTheDatabaseCannotConvert(ExtensionHarness):
    """A property with no clause list still has to be counted exactly once.

    The parser refuses what it cannot convert faithfully rather than guessing.
    Those properties are compared as text, and the loop has to treat them like
    any other: novel once, a duplicate afterwards.
    """

    #: A reduction over a negated operand, one of the shapes the parser refuses
    #: rather than approximate.
    UNREPRESENTABLE = '!tiny.hit_q == !|tiny.mask_q'

    def test_an_unrepresentable_property_is_accumulated(self):
        batch = [assertion('a_sum', self.UNREPRESENTABLE)]
        agent, _model = self.run_extension([REQ_GNT], [batch, None])
        self.assertIsNone(
            agent.parser.parse_sva(agent._parser_input(batch[0]))[2],
            'the sample is no longer unrepresentable; pick another')
        self.assertEqual(sorted(self.written_names()), ['a_gnt', 'a_sum'])

    def test_restating_it_under_a_new_label_ends_the_loop(self):
        first = [assertion('a_sum', self.UNREPRESENTABLE)]
        again = [assertion('a_sum_again', self.UNREPRESENTABLE)]
        agent, model = self.run_extension([REQ_GNT], [first, again, None])
        self.assertEqual(model.calls, 2)
        self.assertEqual(len(agent.assertions), 2)

    def test_text_that_is_not_an_assertion_is_never_accumulated(self):
        batch = ['always_comb x = y;', VALID_DONE]
        agent, _model = self.run_extension([REQ_GNT], [batch, None])
        self.assertEqual(sorted(self.written_names()), ['a_done', 'a_gnt'])


class TestTheExtensionPrompt(ExtensionHarness):
    """What the model is asked, and what is done with what it answers."""

    def make_prompting_agent(self, response, initial=(REQ_GNT,)):
        agent, _model = self.make_agent(list(initial))
        agent._add_assertions = agent_module.CodingAgent._add_assertions.__get__(
            agent)
        self.prompts = []

        def completion(messages, **_kwargs):
            self.prompts.append(messages[-1]['content'])
            return response

        agent._get_llm_completion = completion
        agent.initial_assertions = list(initial)
        agent.llm_stage = 'set_extension'
        return agent

    def test_the_prompt_carries_the_module_the_rtl_and_the_current_set(self):
        agent = self.make_prompting_agent('')
        agent._add_assertions()
        prompt = self.prompts[0]
        self.assertIn('tiny', prompt)
        self.assertIn('module tiny (', prompt)
        self.assertIn('req_i |=> gnt_o', prompt)

    def test_the_prompt_asks_for_new_properties_only(self):
        agent = self.make_prompting_agent('')
        agent._add_assertions()
        self.assertIn('Do not repeat assertions already in the current set',
                      self.prompts[0])

    def test_the_prompt_asks_only_for_the_slots_left_under_the_cap(self):
        self.cap(4)
        agent = self.make_prompting_agent('', initial=(REQ_GNT, VALID_DONE))
        agent._add_assertions()
        prompt = self.prompts[0]
        self.assertIn('capped at 4 assertions', prompt)
        self.assertIn('Generate at most 2 NEW assertions', prompt)

    def test_the_prompt_forbids_module_type_prefixing(self):
        agent = self.make_prompting_agent('')
        agent._add_assertions()
        prompt = self.prompts[0]
        self.assertNotIn('must be prefixed with', prompt)
        self.assertIn('unqualified checker port', prompt)
        self.assertIn('Do NOT write tiny.signal', prompt)

    def test_a_structured_response_becomes_the_next_batch(self):
        agent = self.make_prompting_agent(
            '---\nid: valid_done\nproperty: valid_i |=> done_o\n'
            'failure: valid without done\n')
        agent._add_assertions()
        self.assertEqual(len(agent.assertions), 1)
        self.assertIn('valid_i |=> done_o', agent.assertions[0])
        self.assertEqual(get_assertion_name(agent.assertions[0]),
                         'a_valid_done')

    def test_a_response_with_nothing_usable_leaves_the_set_alone(self):
        agent = self.make_prompting_agent('I cannot help with that.')
        agent._add_assertions()
        self.assertEqual(agent.assertions, [REQ_GNT])

    def test_the_exchange_is_kept_on_disk(self):
        agent = self.make_prompting_agent(
            '---\nid: valid_done\nproperty: valid_i |=> done_o\n'
            'failure: valid without done\n')
        agent.syntax_iterations = 7
        agent._add_assertions()
        stored = os.listdir(os.path.join(self.workdir.name, 'interactions'))
        self.assertIn('tiny_prop_extension7_prompt.txt', stored)
        self.assertIn('tiny_prop_extension7_response.txt', stored)


def load_corpus():
    if not os.path.exists(CORPUS):
        return None
    with open(CORPUS, errors='replace') as handle:
        return [line.strip() for line in handle
                if line.strip() and not line.lstrip().startswith('//')]


class TestTheLoopOnRealAssertions(ExtensionHarness):
    """The loop driven by assertions SVApshot actually wrote.

    The shapes here are not chosen to be convenient: they carry hierarchical
    references, sampled values, comparisons and reductions, which is what the
    duplicate question has to survive for the stop condition to mean anything.
    """

    SLICE = 12

    @classmethod
    def setUpClass(cls):
        corpus = load_corpus()
        if corpus is None:
            raise unittest.SkipTest(
                f'assertion corpus collateral is missing: {CORPUS}')
        step = max(1, len(corpus) // cls.SLICE)
        cls.sample = [line for line in corpus[::step]][:cls.SLICE]

    def room_for_the_sample(self):
        """A cap high enough that only the stop condition can end the loop."""
        self.cap(len(self.sample) * 3)

    def relabelled(self, assertions, tag):
        return [re.sub(r'^\s*\w+\s*:', f'{tag}_{index}:', text)
                for index, text in enumerate(assertions)]

    def test_a_real_batch_is_accumulated(self):
        self.room_for_the_sample()
        agent, _model = self.run_extension([REQ_GNT], [self.sample, None])
        self.assertEqual(len(agent.assertions), len(self.sample) + 1)

    def test_replaying_the_batch_relabelled_adds_nothing(self):
        self.room_for_the_sample()
        agent, model = self.run_extension(
            [REQ_GNT],
            [self.sample, self.relabelled(self.sample, 'again'), None])
        self.assertEqual(model.calls, 2)
        self.assertEqual(len(agent.assertions), len(self.sample) + 1)

    def test_replaying_the_batch_backwards_adds_nothing(self):
        self.room_for_the_sample()
        agent, model = self.run_extension(
            [REQ_GNT], [self.sample, list(reversed(self.sample)), None])
        self.assertEqual(model.calls, 2)
        self.assertEqual(len(agent.assertions), len(self.sample) + 1)

    def test_a_model_that_only_restates_stops_the_loop_at_once(self):
        self.room_for_the_sample()
        agent, _model = self.make_agent(self.sample)
        agent._extend_property_set()
        model = ScriptedModel([self.relabelled(self.sample, 'restated')])
        agent._add_assertions = lambda: model.respond(agent)
        agent._extend_property_set()
        self.assertEqual(model.calls, 1)
        self.assertEqual(len(agent.assertions), len(self.sample))


class TestAssertionCap(unittest.TestCase):

    def tearDown(self):
        os.environ.pop('SVAPSHOT_MAX_ASSERTIONS', None)

    def test_zero_means_uncapped(self):
        os.environ['SVAPSHOT_MAX_ASSERTIONS'] = '0'
        self.assertIsNone(agent_module.assertion_cap())

    def test_unlimited_token_means_uncapped(self):
        os.environ['SVAPSHOT_MAX_ASSERTIONS'] = 'unlimited'
        self.assertIsNone(agent_module.assertion_cap())

    def test_positive_override(self):
        os.environ['SVAPSHOT_MAX_ASSERTIONS'] = '12'
        self.assertEqual(agent_module.assertion_cap(), 12)


class TestAssumptionProposalRetries(unittest.TestCase):
    """Empty assumption replies must be retried; they are not a none-decision."""

    def setUp(self):
        self.stored = []

    def _agent(self, replies):
        agent = agent_module.CodingAgent.__new__(agent_module.CodingAgent)
        agent.rtl = RTL
        agent.rtl_module = 'tiny.sv'
        agent.active_assumptions = []
        agent.formal_result = None
        agent.verbosity = 0
        agent.llm_stage = ''
        agent._log = lambda *args, **kwargs: None
        agent._load_repair_cycle_table = lambda *args, **kwargs: None
        agent._store_llm_interaction = (
            lambda stage, iteration, prompt, response, assertion_name=None:
            self.stored.append((iteration, response)))
        agent._extract_module_name = (
            agent_module.CodingAgent._extract_module_name.__get__(agent))
        queue = list(replies)

        def completion(_messages, **_kwargs):
            return queue.pop(0) if queue else ''

        agent._get_llm_completion = completion
        return agent

    def test_empty_assumption_reply_is_retried(self):
        agent = self._agent([
            '',
            '---\n'
            'id: req_held\n'
            'property: req_i && !gnt_o |=> req_i\n'
            'rationale: Master holds request until grant\n'
            'targets: a_x\n',
        ])
        baseline = type('Baseline', (), {'records': {}})()
        candidates = agent._propose_assumptions(
            baseline, [('a_x', 'a_x: assert property (req_i |=> gnt_o);')])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].expression, 'req_i && !gnt_o |=> req_i')
        self.assertEqual(len(self.stored), 2)

    def test_explicit_none_stops_without_another_ask(self):
        agent = self._agent([
            '---\n'
            'decision: none\n'
            'rationale: Legal handshake inputs\n',
            'should-not-be-called',
        ])
        baseline = type('Baseline', (), {'records': {}})()
        candidates = agent._propose_assumptions(
            baseline, [('a_x', 'a_x: assert property (req_i |=> gnt_o);')])
        self.assertEqual(candidates, [])
        self.assertEqual(len(self.stored), 1)

    def test_responses_output_text_walks_message_parts(self):
        part = type('Part', (), {'text': 'id: x\nproperty: req_i |=> gnt_o'})()
        item = type('Item', (), {'content': [part]})()
        completion = type('C', (), {'output_text': '', 'output': [item]})()
        self.assertIn(
            'req_i |=> gnt_o',
            agent_module.responses_output_text(completion))


if __name__ == '__main__':
    unittest.main(verbosity=2)
