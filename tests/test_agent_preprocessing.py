#!/usr/bin/env python3
"""Unit tests for density-safe internal port promotion + bind wiring.

The preprocessing boundary has two responsibilities before linting:

* promote references to DUT-internal signals to checker ports (unqualified
  names in properties; prop port list + bind updates), without emitting
  MODULE.signal;
* make assertion labels unique without dropping or rewriting any property.

The tests instantiate ``CodingAgent`` without running its constructor: no LLM,
formal tool, property file, API key, or licence is involved.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest

import _path_setup  # noqa: F401, E402

import agent as agent_module
import bind_internals
import svaparser
from assertion_template import assemble_assertion, get_assertion_name


SARGANTANA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'benchmarks', 'sargantana')
DIV_UNIT = os.path.join(
    SARGANTANA, 'rtl/datapath/rtl/exe_stage/rtl/div_unit.sv')


RTL = """\
module tiny #(
    parameter int width = 8,
    parameter int DEPTH = 4,
    parameter type data_t = logic [width-1:0]
) (
    input  logic                 clk_i,
    input  logic                 ready_q,
    input  logic                 valid_q,
    input  logic [width-1:0]     data_i,
    output logic [width-1:0]     data_o
);
    typedef enum logic [1:0] {IDLE, RUN, WAIT_D} state_t;
    localparam int last_index = width - 1;
    localparam int LIMIT = DEPTH - 1;

    logic [width-1:0] data_q, data_d;
    logic [width-1:0] buffer [DEPTH];
    logic             helper;
    logic             helper_s1;
    state_t           state_q, state_d;

    always_ff @(posedge clk_i) begin
        data_q  <= data_d;
        state_q <= state_d;
    end
endmodule
"""

CHECKER = """\
module tiny_prop #(
    parameter ASSERT_INPUTS = 0,
    parameter int width = 8,
    parameter int DEPTH = 4
) (
    input  logic             clk_i,
    input  logic             ready_q,
    input  logic             valid_q,
    input  logic [width-1:0] data_i,
    input  logic [width-1:0] data_o //output
);
//====DESIGNER-ADDED-SVA====//
"""

BIND = """\
bind tiny tiny_prop
	#(
		.width (width),
		.DEPTH (DEPTH),
		.ASSERT_INPUTS (0)
	) u_tiny_sva(.*);
"""


def assertion(name: str, prop: str, failure: str | None = None) -> str:
    return assemble_assertion(name, prop, failure or f'{name} failed')


def make_agent(rtl: str = RTL, module: str = 'tiny.sv',
               checker: str = CHECKER, bind_text: str = BIND):
    """Create only the state the two preprocessing methods consume."""
    instance = agent_module.CodingAgent.__new__(agent_module.CodingAgent)
    instance.rtl = rtl
    instance.rtl_module = module
    instance.checker_code = checker
    instance._log = lambda *_args, **_kwargs: None
    instance._hierarchical_bind_overrides = {}
    instance._pending_density_probe_signals = set()
    instance.workdir = tempfile.TemporaryDirectory(prefix='bind_ports_')
    instance.assertions_dir = instance.workdir.name + os.sep
    bind_path = os.path.join(instance.assertions_dir, 'tiny_bind.svh')
    with open(bind_path, 'w', encoding='utf-8') as handle:
        handle.write(bind_text)
    return instance


def promote(instance, *assertions):
    return instance._promote_internal_checker_ports(list(assertions))


def dedupe(instance, *assertions):
    return instance._fix_duplicate_assertion_names(list(assertions))


class TestSignalDiscoveryAndPortPromotion(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def tearDown(self):
        self.agent.workdir.cleanup()

    def test_a_declared_register_becomes_an_unqualified_port(self):
        result, count, fixed = promote(
            self.agent, assertion('state', 'state_q == RUN'))
        self.assertIn('state_q == RUN', result[0])
        self.assertNotIn('tiny.state_q', result[0])
        self.assertNotIn('tiny.RUN', result[0])
        self.assertIn('state_q', fixed)
        self.assertGreaterEqual(count, 1)
        self.assertIn('input', self.agent.checker_code)
        self.assertIn('state_q', self.agent.checker_code)

    def test_module_prefixed_internals_are_stripped(self):
        result, count, fixed = promote(
            self.agent, assertion('state', 'tiny.state_q == tiny.RUN'))
        self.assertIn('state_q == RUN', result[0])
        self.assertNotIn('tiny.state_q', result[0])
        self.assertIn('state_q', fixed)
        self.assertGreaterEqual(count, 1)

    def test_a_double_module_prefix_is_collapsed_to_unqualified(self):
        result, _, fixed = promote(
            self.agent, assertion('double', 'tiny.tiny.state_q == tiny.RUN'))
        self.assertIn('state_q == RUN', result[0])
        self.assertNotIn('tiny.tiny.', result[0])
        self.assertNotIn('tiny.state_q', result[0])
        self.assertIn('state_q', fixed)

    def test_an_internal_without_a_naming_suffix_is_promoted(self):
        result, count, fixed = promote(
            self.agent, assertion('helper', 'helper |-> data_o'))
        self.assertIn('(helper |-> data_o)', result[0])
        self.assertNotIn('tiny.helper', result[0])
        self.assertEqual(fixed, {'helper'})
        self.assertEqual(count, 1)

    def test_unknown_q_and_d_names_are_not_invented(self):
        result, count, fixed = promote(
            self.agent, assertion('unknown', 'misspelled_q == ghost_d'))
        self.assertIn('(misspelled_q == ghost_d)', result[0])
        self.assertEqual((count, fixed), (0, set()))

    def test_all_occurrences_stay_unqualified(self):
        result, _, fixed = promote(
            self.agent,
            assertion('repeat', 'data_q == $past(data_q) && data_q != data_d'),
        )
        self.assertEqual(result[0].count('data_q'), 3)
        self.assertNotIn('tiny.data_q', result[0])
        self.assertEqual(fixed, {'data_q', 'data_d'})

    def test_a_numeric_bit_select_keeps_the_index(self):
        result, count, _ = promote(
            self.agent, assertion('bit', 'data_q[3] == data_i[3]'))
        self.assertIn('data_q[3] == data_i[3]', result[0])
        self.assertNotIn('tiny.data_q', result[0])
        self.assertEqual(count, 1)

    def test_parameters_are_unqualified_without_new_ports(self):
        result, count, fixed = promote(
            self.agent,
            assertion('slice', 'data_q[width-1:0] == data_i[width-1:0]'),
        )
        self.assertIn('data_q[width-1:0] == data_i[width-1:0]', result[0])
        self.assertNotIn('tiny.width', result[0])
        self.assertIn('data_q', fixed)
        self.assertNotIn('width', fixed)

    def test_multiple_ports_on_adjacent_lines_remain_unqualified(self):
        result, count, fixed = promote(
            self.agent,
            assertion('ports', 'ready_q && valid_q |=> data_o == data_i'),
        )
        self.assertIn('ready_q && valid_q |=> data_o == data_i', result[0])
        self.assertEqual((count, fixed), (0, set()))

    def test_an_existing_unqualified_internal_is_idempotent_on_text(self):
        source = assertion('qualified', 'state_q == RUN')
        first, first_count, first_fixed = promote(self.agent, source)
        second, second_count, second_fixed = promote(self.agent, *first)
        self.assertEqual(first[0], second[0])
        self.assertIn('state_q', first_fixed)
        # Second pass finds the port already present; still reports the bind.
        self.assertEqual(second[0], first[0])
        self.assertGreaterEqual(first_count, 1)

    def test_a_reference_through_another_instance_is_untouched(self):
        result, count, fixed = promote(
            self.agent, assertion('other', 'u_child.state_q == RUN'))
        self.assertIn('u_child.state_q == RUN', result[0])
        self.assertNotIn('u_child.tiny.state_q', result[0])
        self.assertNotIn('state_q', fixed)

    def test_a_struct_field_with_the_same_name_is_untouched(self):
        result, count, fixed = promote(
            self.agent, assertion('field', 'data_i.state_q == state_q'))
        self.assertIn('data_i.state_q == state_q', result[0])
        self.assertEqual(fixed, {'state_q'})

    def test_a_named_port_argument_stays_unqualified(self):
        result, count, fixed = promote(
            self.agent, assertion('named', '.state_q(state_q)'))
        self.assertIn('.state_q(state_q)', result[0])
        self.assertEqual(fixed, {'state_q'})

    def test_a_package_scoped_enum_is_not_requalified(self):
        result, count, fixed = promote(
            self.agent, assertion('package', 'state_q == states_pkg::RUN'))
        self.assertIn('state_q == states_pkg::RUN', result[0])
        self.assertNotIn('states_pkg::tiny.RUN', result[0])
        self.assertEqual(fixed, {'state_q'})

    def test_system_functions_are_preserved(self):
        result, _, fixed = promote(
            self.agent, assertion('past', '$past(state_q) == RUN'))
        self.assertIn('$past(state_q) == RUN', result[0])
        self.assertNotIn('tiny.past', result[0])
        self.assertIn('state_q', fixed)

    def test_the_assertion_label_is_never_rewritten(self):
        source = assertion('state_q', 'state_q == RUN')
        result, _, _ = promote(self.agent, source)
        self.assertEqual(get_assertion_name(result[0]), 'a_state_q')
        self.assertNotIn('tiny.a_state_q:', result[0])

    def test_failure_text_is_never_rewritten(self):
        source = assertion(
            'message', 'state_q == RUN',
            'state_q should enter RUN; tiny.state_q is the DUT signal',
        )
        result, _, _ = promote(self.agent, source)
        self.assertIn(
            '$display("state_q should enter RUN; tiny.state_q is the DUT signal")',
            result[0],
        )

    def test_line_comments_are_never_rewritten(self):
        source = (
            'a_comment: assert property ((state_q == RUN)) // state_q and RUN\n'
            'else begin\n'
            '    $display("failed");\n'
            'end'
        )
        result, _, _ = promote(self.agent, source)
        self.assertIn('// state_q and RUN', result[0])
        self.assertIn('state_q == RUN', result[0])

    def test_malformed_text_without_a_property_is_unchanged(self):
        source = 'state_q == RUN'
        result, count, fixed = promote(self.agent, source)
        self.assertEqual(result, [source])
        self.assertEqual((count, fixed), (0, set()))

    def test_processing_does_not_mutate_the_input_list(self):
        source = assertion('state', 'state_q == RUN')
        values = [source]
        promote(self.agent, *values)
        self.assertEqual(values, [source])

    def test_same_name_bind_keeps_dot_star(self):
        promote(self.agent, assertion('state', 'state_q == RUN'))
        with open(
                os.path.join(self.agent.assertions_dir, 'tiny_bind.svh'),
                encoding='utf-8') as handle:
            bind_text = handle.read()
        self.assertIn('.*', bind_text)
        self.assertNotIn('.state_q(', bind_text)


@unittest.skipUnless(os.path.isfile(DIV_UNIT), 'Sargantana div_unit is absent')
class TestPortPromotionOnSargantana(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(DIV_UNIT, encoding='utf-8', errors='replace') as handle:
            cls.rtl = handle.read()

    def setUp(self):
        checker = (
            'module div_unit_prop (\n'
            '\t\tinput logic clk_i,\n'
            '\t\tinput logic instruction_i,\n'
            '\t\tinput logic instruction_o,\n'
            '\t\tinput logic div_unit_sel_i\n'
            '\t);\n'
            '//====DESIGNER-ADDED-SVA====//'
        )
        bind_text = 'bind div_unit div_unit_prop u_div_unit_sva(.*);'
        self.agent = make_agent(self.rtl, DIV_UNIT, checker, bind_text)
        # Bind file name follows module basename.
        os.rename(
            os.path.join(self.agent.assertions_dir, 'tiny_bind.svh'),
            os.path.join(self.agent.assertions_dir, 'div_unit_bind.svh'),
        )

    def tearDown(self):
        self.agent.workdir.cleanup()

    def test_real_ports_stay_unqualified_and_internals_are_promoted(self):
        source = assertion(
            'pipeline',
            ('instruction_i.instr.valid |=> '
             'instruction_q[0].valid && data_src1 == instruction_i.data_rs1'),
        )
        result, count, fixed = promote(self.agent, source)
        self.assertIn('instruction_i.instr.valid', result[0])
        self.assertIn('instruction_q[0].valid', result[0])
        self.assertIn('data_src1 == instruction_i.data_rs1', result[0])
        self.assertNotIn('div_unit.instruction_q', result[0])
        self.assertEqual(fixed, {'instruction_q', 'data_src1'})
        self.assertEqual(count, 2)


class TestDuplicateAssertionNames(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def tearDown(self):
        self.agent.workdir.cleanup()

    def test_unique_names_are_returned_byte_for_byte(self):
        values = [assertion('first', 'data_q'), assertion('second', 'state_q')]
        result, count, renamed = dedupe(self.agent, *values)
        self.assertEqual(result, values)
        self.assertEqual((count, renamed), (0, {}))

    def test_the_second_occurrence_gets_suffix_one(self):
        first = assertion('same', 'data_q')
        second = assertion('same', 'state_q')
        result, count, renamed = dedupe(self.agent, first, second)
        self.assertEqual(
            [get_assertion_name(item) for item in result],
            ['a_same', 'a_same_1'],
        )
        self.assertEqual(count, 1)
        self.assertEqual(renamed, {'a_same_occurrence_1': 'a_same_1'})

    def test_the_property_file_writer_uses_the_same_collision_safe_rule(self):
        values = [
            assertion('same', 'data_q'),
            assertion('same', 'state_q'),
            assertion('same_1', 'helper'),
        ]
        with tempfile.TemporaryDirectory(prefix='agent_names_') as directory:
            self.agent.assertions = values
            self.agent.checker_code = 'module tiny_prop;\n'
            self.agent.assertions_dir = directory + os.sep
            self.agent.assertions_file = 'tiny_prop.sv'
            self.agent.commented_assertions = []
            self.agent.active_assumptions = []
            self.agent._write_assertions()
            with open(
                    os.path.join(directory, 'tiny_prop.sv'),
                    encoding='utf-8') as handle:
                written = handle.read()

        names = re.findall(
            r'^([A-Za-z_]\w*)\s*:\s*assert\s+property',
            written,
            re.MULTILINE,
        )
        self.assertEqual(names, ['a_same', 'a_same_2', 'a_same_1'])
        self.assertEqual(len(names), len(set(names)))


class TestPreprocessingHelpers(unittest.TestCase):
    def test_literal_fragments_are_recognised(self):
        for value in ('b0', 'B10xz', 'hff', 'd123', 'o77', '0', 'b'):
            with self.subTest(value=value):
                self.assertTrue(
                    agent_module._looks_like_sv_literal_fragment(value))

    def test_signal_names_are_not_literal_fragments(self):
        for value in ('buffer', 'data_q', 'hex_value', 'done'):
            with self.subTest(value=value):
                self.assertFalse(
                    agent_module._looks_like_sv_literal_fragment(value))

    def test_a_bare_pattern_ignores_hierarchical_and_package_qualification(self):
        pattern = agent_module._module_prefix_pattern('tiny', 'RUN')
        source = 'RUN tiny.RUN child.RUN states_pkg::RUN'
        matches = [match.group(0) for match in re.finditer(pattern, source)]
        self.assertEqual(matches, ['RUN'])


class TestAgentHintContract(unittest.TestCase):
    """Prefix advice must not regress against the density-safe prompt."""

    def test_agent_source_does_not_advise_module_prefixing(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'src', 'core', 'agent.py',
        )
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
        self.assertNotIn('must be prefixed with', text)
        self.assertNotIn('correct module prefix for internal', text)
        self.assertIn('unqualified checker port', text)
        self.assertIn('internal_port_binds', text)
        self.assertNotIn("'module_prefix_fixes'", text)


class TestHandoffToTheAssertionDatabase(unittest.TestCase):
    """What the parser is given is the property, and nothing around it."""

    def parser_input(self, assertion: str) -> str:
        return agent_module.CodingAgent._parser_input(assertion)

    def test_the_failure_action_is_not_handed_over(self):
        text = self.parser_input(
            'a_gnt: assert property (req_i |=> gnt_o) '
            'else begin $display("Assertion a_gnt failed"); end')
        self.assertEqual(text, 'a_gnt:assertproperty(req_i|=>gnt_o);')

    def test_the_database_sees_one_property_either_way(self):
        parser = svaparser.SVAParser()
        with_action = ('a_one: assert property (req_i |=> gnt_o) '
                       'else begin $display("failed"); end')
        without_action = 'a_two: assert property (req_i |=> gnt_o);'
        self.assertEqual(
            parser.process_property(self.parser_input(with_action)), 1)
        self.assertEqual(
            parser.process_property(self.parser_input(without_action)), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
