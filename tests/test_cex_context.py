#!/usr/bin/env python3
"""Contract tests for CEX-aware semantic-repair prompt context.

Repair used to paste the failing SVA and the whole RTL. The formal tool
already knows the failure kind, bound, engine, assumptions, formal core,
and sibling proofs; isolated repair now dumps a trace. These tests pin the
six prompt hints plus the cycle table without invoking ``vcf`` or an LLM.

    python3 tests/test_cex_context.py
"""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace

import _path_setup  # noqa: F401

import agent as agent_module
import cex_context
from proof_status import FormalRunResult, ProofStatus, PropertyRecord, VacuityStatus

PROP = (
    'a_req_gnt: assert property (@(posedge clk_i) disable iff (!rst_ni)\n'
    '    req_i |-> ##1 gnt_o);\n'
)

PROVEN = (
    'a_busy_holds: assert property (@(posedge clk_i)\n'
    '    busy_q |-> !idle_o);\n'
)

RTL = """\
module tiny (
    input  logic clk_i,
    input  logic rst_ni,
    input  logic req_i,
    output logic gnt_o,
    output logic idle_o
);
    logic busy_q;
    always_ff @(posedge clk_i) begin
        if (!rst_ni) busy_q <= 1'b0;
        else busy_q <= req_i;
    end
    assign gnt_o = busy_q;
    assign idle_o = !busy_q;
endmodule

module unrelated_fsm (
    input logic clk_i,
    output logic state_q
);
    always_ff @(posedge clk_i) state_q <= ~state_q;
endmodule
"""

VCD = """\
$timescale 1ns $end
$scope module tiny $end
$var wire 1 ! req_i $end
$var wire 1 " gnt_o $end
$var wire 1 # busy_q $end
$var wire 1 $ idle_o $end
$upscope $end
$enddefinitions $end
$dumpvars
0!
0"
0#
1$
$end
#0
#10
1!
#20
1#
0$
#30
0"
"""


def falsified_record(**overrides):
    fields = dict(
        name='a_req_gnt',
        proof_status=ProofStatus.FALSIFIED,
        vacuity_status=VacuityStatus.NON_VACUOUS,
        engine='HP',
        bound_depth=3,
        formal_core_input_names=['tiny.req_i'],
        formal_core_register_names=['tiny.busy_q'],
        coi_objects=['tiny.gnt_o'],
    )
    fields.update(overrides)
    return PropertyRecord(**fields)


def vacuous_record():
    return PropertyRecord(
        name='a_req_gnt',
        proof_status=ProofStatus.PROVEN,
        vacuity_status=VacuityStatus.VACUOUS,
        engine='HP',
        bound_depth=0,
        formal_core_input_names=['tiny.req_i'],
    )


class TestDepthAndFailureKind(unittest.TestCase):

    def test_depth_zero_is_combinational(self):
        self.assertIn('combinational', cex_context.describe_depth(0))

    def test_depth_one_is_one_cycle(self):
        self.assertIn('1-cycle', cex_context.describe_depth(1))

    def test_depth_three_names_the_path(self):
        text = cex_context.describe_depth(3)
        self.assertIn('3-cycle', text)
        self.assertIn('depth 3', text)

    def test_falsified_and_vacuous_are_different_instructions(self):
        falsified = cex_context.describe_failure(falsified_record())
        vacuous = cex_context.describe_failure(vacuous_record())
        self.assertIn('FALSIFIED', falsified)
        self.assertIn('consequent', falsified.lower())
        self.assertIn('VACUOUS', vacuous)
        self.assertIn('antecedent', vacuous.lower())
        self.assertNotEqual(falsified, vacuous)

    def test_missing_assumption_is_named_on_a_falsified_record(self):
        record = falsified_record(missing_assumptions=['m_legal_req'])
        self.assertIn('m_legal_req', cex_context.describe_failure(record))


class TestAssumptionsCoreSiblingsProgress(unittest.TestCase):

    def test_active_assumptions_are_listed(self):
        assumption = SimpleNamespace(
            name='m_stable_req', expression='req_i |-> ##1 req_i')
        text = cex_context.format_assumptions([assumption])
        self.assertIn('already in force', text)
        self.assertIn('m_stable_req', text)
        self.assertIn('req_i |-> ##1 req_i', text)

    def test_no_assumptions_is_explicit(self):
        self.assertIn('none', cex_context.format_assumptions([]))

    def test_formal_core_and_coi_are_leaf_names(self):
        names = cex_context.core_and_coi_signals(falsified_record())
        self.assertEqual(names, ['req_i', 'busy_q', 'gnt_o'])
        section = cex_context.format_core_section(falsified_record())
        self.assertIn('req_i', section)
        self.assertIn('prefer these', section.lower())

    def test_sibling_proofs_are_quoted(self):
        text = cex_context.format_siblings([PROVEN])
        self.assertIn('already proved', text)
        self.assertIn('busy_q', text)

    def test_same_signature_asks_for_a_different_kind_of_edit(self):
        sig = 'falsified@3/HP'
        text = cex_context.solver_progress(sig, sig)
        self.assertIn('did not change', text)
        self.assertIn('delay', text)
        self.assertIn('polarity', text)
        self.assertIn('split', text)

    def test_moved_signature_reports_both_ends(self):
        text = cex_context.solver_progress('falsified@3/HP', 'falsified@5/HP')
        self.assertIn('falsified@3/HP', text)
        self.assertIn('falsified@5/HP', text)

    def test_first_attempt_has_no_progress_line(self):
        self.assertEqual(cex_context.solver_progress(None, 'falsified@3/HP'), '')


class TestCycleTable(unittest.TestCase):

    def test_vcd_becomes_a_cycle_diagram_with_the_fail_row_marked(self):
        table = cex_context.parse_vcd_table(
            VCD, ['req_i', 'gnt_o', 'busy_q'],
            fail_cycle=3, fail_signal='gnt_o', source='a_req_gnt.vcd')
        self.assertEqual(len(table.cycles), 4)
        self.assertEqual(table.cycles[0]['req_i'], '0')
        self.assertEqual(table.cycles[1]['req_i'], '1')
        self.assertEqual(table.cycles[3]['gnt_o'], '0')
        diagram = cex_context.format_cycle_table(table)
        self.assertIn('CEX time diagram', diagram)
        self.assertIn('req_i', diagram)
        self.assertIn('← fail', diagram)
        self.assertIn('gnt_o', diagram)

    def test_text_table_round_trips(self):
        raw = (
            't req_i gnt_o busy_q\n'
            '0 0 0 0\n'
            '1 1 0 0\n'
            '2 1 0 1\n'
            '3 1 0 1  ← fail\n'
            'fail_signal=gnt_o\n'
        )
        table = cex_context.parse_text_table(raw)
        self.assertEqual(table.fail_cycle, 3)
        self.assertEqual(table.fail_signal, 'gnt_o')
        self.assertEqual(table.cycles[1]['req_i'], '1')

    def test_load_prefers_txt_over_vcd(self):
        with tempfile.TemporaryDirectory() as tmp:
            cex_dir = os.path.join(tmp, 'cex')
            os.makedirs(cex_dir)
            with open(os.path.join(cex_dir, 'a_req_gnt.txt'), 'w') as handle:
                handle.write('t req_i\n0 1\n')
            with open(os.path.join(cex_dir, 'a_req_gnt.vcd'), 'w') as handle:
                handle.write(VCD)
            table = cex_context.load_cycle_table(tmp, 'a_req_gnt', ['req_i'])
            self.assertEqual(table.source, 'text')
            self.assertEqual(table.cycles[0]['req_i'], '1')

    def test_load_reads_vcd_when_txt_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cex_dir = os.path.join(tmp, 'cex')
            os.makedirs(cex_dir)
            with open(os.path.join(cex_dir, 'a_req_gnt.vcd'), 'w') as handle:
                handle.write(VCD)
            table = cex_context.load_cycle_table(tmp, 'a_req_gnt', ['req_i'])
            self.assertTrue(table.cycles)
            self.assertEqual(table.source, 'a_req_gnt.vcd')

    def test_vacuous_guidance_has_no_waveform(self):
        block = cex_context.build_repair_context_block(
            record=vacuous_record(),
            cycle_table=None,
            property_text=PROP,
        )
        self.assertIn('VACUOUS', block)
        self.assertIn('none (vacuous', block)
        self.assertNotIn('← fail', block)
        self.assertIn('Do not tighten the consequent', block)

    def test_skeleton_table_uses_depth_when_no_dump_exists(self):
        table = cex_context.skeleton_cycle_table(['req_i', 'gnt_o'], 3)
        self.assertEqual(len(table.cycles), 4)
        self.assertEqual(table.cycles[0]['req_i'], '?')
        self.assertEqual(table.fail_cycle, 3)
        self.assertIn('depth-only', table.source)


class TestRtlFocusAndAssembledBlock(unittest.TestCase):

    def test_property_signals_drop_keywords_and_keep_ports(self):
        names = cex_context.signals_from_property(PROP)
        self.assertIn('req_i', names)
        self.assertIn('gnt_o', names)
        self.assertIn('clk_i', names)
        self.assertNotIn('assert', names)
        self.assertNotIn('property', names)
        self.assertNotIn('posedge', names)

    def test_rtl_focus_keeps_core_signals_and_drops_an_unrelated_fsm(self):
        focused = cex_context.rtl_focus(RTL, ['req_i', 'gnt_o', 'busy_q'])
        self.assertIn('req_i', focused)
        self.assertIn('busy_q', focused)
        self.assertNotIn('unrelated_fsm', focused)
        self.assertNotIn('state_q', focused)

    def test_rtl_focus_returns_the_original_when_nothing_matches(self):
        self.assertEqual(cex_context.rtl_focus(RTL, ['no_such_port']), RTL)

    def test_assembled_block_carries_all_six_hints_and_the_diagram(self):
        table = cex_context.parse_vcd_table(
            VCD, ['req_i', 'gnt_o', 'busy_q'],
            fail_cycle=3, fail_signal='gnt_o')
        assumption = SimpleNamespace(name='m_stable_req', expression='1')
        block = cex_context.build_repair_context_block(
            record=falsified_record(),
            assumptions=[assumption],
            proven_assertions=[PROVEN],
            previous_signature='falsified@3/HP',
            latest_signature='falsified@3/HP',
            cycle_table=table,
            property_text=PROP,
        )
        self.assertIn('SEMANTIC REPAIR CONTEXT', block)
        self.assertIn('FALSIFIED', block)
        self.assertIn('m_stable_req', block)
        self.assertIn('req_i', block)
        self.assertIn('busy_q', block)
        self.assertIn('already proved', block)
        self.assertIn('did not change', block)
        self.assertIn('CEX time diagram', block)
        self.assertIn('real counterexample', block.lower())
        omitted = cex_context.build_repair_context_block(
            record=falsified_record(),
            assumptions=[assumption],
            proven_assertions=[PROVEN],
            previous_signature='falsified@3/HP',
            latest_signature='falsified@3/HP',
            cycle_table=table,
            property_text=PROP,
            include_cex=False,
        )
        self.assertIn('SEMANTIC REPAIR CONTEXT', omitted)
        self.assertIn('FALSIFIED', omitted)
        self.assertIn('omitted (no_cex_in_prompt)', omitted)
        self.assertNotIn('← fail', omitted)
        self.assertNotIn('CEX time diagram is no longer a violation', omitted)
        self.assertIn('No CEX traces are provided', omitted)

    def test_assumption_cex_includes_the_diagram(self):
        record = falsified_record()
        table = cex_context.parse_vcd_table(
            VCD, ['req_i'], fail_cycle=3, fail_signal='gnt_o')
        text = cex_context.format_assumption_cex(
            {'a_req_gnt': record}, ['a_req_gnt'], {'a_req_gnt': table})
        self.assertIn('FALSIFIED', text)
        self.assertIn('CEX time diagram', text)


class TestAgentPromptInjection(unittest.TestCase):
    """The repair helpers on CodingAgent consume the context builder."""

    def make_agent(self, record, report_dir=None):
        agent = agent_module.CodingAgent.__new__(agent_module.CodingAgent)
        agent.rtl = RTL
        agent.rtl_module = 'tiny.sv'
        agent.formal_tool = 'vcformal'
        agent.active_assumptions = [
            SimpleNamespace(name='m_stable_req', expression='req_i |-> ##1 req_i'),
        ]
        agent.formal_result = FormalRunResult(records={record.name: record})
        if report_dir is not None:
            agent._get_formal_report_dir = lambda: report_dir
        else:
            agent._get_formal_report_dir = lambda: '/no/such/reports'
        return agent

    def test_repair_block_lists_assumptions_siblings_and_progress(self):
        agent = self.make_agent(falsified_record())
        block = agent._semantic_repair_context_block(
            'a_req_gnt', PROP,
            proven_assertions=[PROVEN],
            previous_signature='falsified@3/HP',
        )
        self.assertIn('m_stable_req', block)
        self.assertIn('FALSIFIED', block)
        self.assertIn('already proved', block)
        self.assertIn('did not change', block)
        self.assertIn('CEX time diagram', block)

    def test_vacuous_repair_does_not_invent_a_waveform(self):
        agent = self.make_agent(vacuous_record())
        block = agent._semantic_repair_context_block('a_req_gnt', PROP)
        self.assertIn('VACUOUS', block)
        self.assertIn('none (vacuous', block)

    def test_no_cex_ablation_omits_the_diagram(self):
        agent = self.make_agent(falsified_record())
        agent.ablation = 'no_cex_in_prompt'

        def _boom(*_args, **_kwargs):
            raise AssertionError('CEX dump must not be loaded')

        agent._load_repair_cycle_table = _boom
        block = agent._semantic_repair_context_block(
            'a_req_gnt', PROP,
            proven_assertions=[PROVEN],
            previous_signature='falsified@3/HP',
        )
        self.assertIn('no_cex_in_prompt', block)
        self.assertNotIn('← fail', block)
        self.assertFalse(agent._include_cex_in_prompt())

    def test_focused_rtl_drops_the_unrelated_fsm(self):
        agent = self.make_agent(falsified_record())
        focused = agent._focused_rtl_for_repair(PROP)
        self.assertIn('req_i', focused)
        self.assertNotIn('unrelated_fsm', focused)

    def test_dumped_vcd_is_preferred_over_the_skeleton(self):
        with tempfile.TemporaryDirectory() as tmp:
            cex_dir = os.path.join(tmp, 'cex')
            os.makedirs(cex_dir)
            with open(os.path.join(cex_dir, 'a_req_gnt.vcd'), 'w') as handle:
                handle.write(VCD)
            agent = self.make_agent(falsified_record(), report_dir=tmp)
            table = agent._load_repair_cycle_table('a_req_gnt', PROP)
            self.assertEqual(table.source, 'a_req_gnt.vcd')
            self.assertEqual(table.cycles[1]['req_i'], '1')


if __name__ == '__main__':
    unittest.main()
