#!/usr/bin/env python3
"""Unit tests for the pure (tool-free) SVApshot modules.

Everything exercised here runs without a formal tool, an LLM, or a licence, so
the invariants the pipeline depends on can be checked in a second:

* the log parser's five-way qualification, including vacuity,
* cone-of-influence extraction and diversity,
* the mutation engine's fault-class coverage, uniqueness, and its refusal to
  mutate verification code or code the elaboration switches off,
* the adaptive stopping policy's early-stop conditions,
* cost accounting and the LLM Efficiency Score,
* contract extraction from a property file.

Run with ``python3 tests/test_svapshot.py``.
"""

from __future__ import annotations

import os
import sys
import unittest

import _path_setup  # noqa: F401, E402

import adaptive_stop
import assumption_gen
import baselines
import coi
import evaluate
import formal_harness
import llm_cost
import metrics_report
import mutation
import proof_status
from proof_status import ProofStatus, Qualification, VacuityStatus


SMALL_RTL = """\
// A two-branch arbiter used by the tests.
module tiny_arb #(
    parameter int unsigned NumIn = 4,
    parameter bit          Fast  = 1'b0
) (
    input  logic             clk_i,
    input  logic             rst_ni,
    input  logic [NumIn-1:0] req_i,
    input  logic             gnt_i,
    output logic             req_o,
    output logic [NumIn-1:0] gnt_o
);

    logic [NumIn-1:0] gnt_d, gnt_q;

    assign req_o = |req_i;
    assign gnt_o = gnt_q;

    always_comb begin
        gnt_d = '0;
        if (req_o && gnt_i) begin
            gnt_d = req_i;
        end
    end

    if (Fast) begin : gen_fast
        assign gnt_d = req_i;
    end else begin : gen_slow
        logic unused_slow;
        assign unused_slow = 1'b0;
    end

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            gnt_q <= '0;
        end else begin
            gnt_q <= gnt_d;
        end
    end

    a_native: assert property (@(posedge clk_i) req_o |-> |req_i)
        else $fatal(1, "broken \\
                        continued message");

endmodule
"""


PROPERTY_FILE = """\
module tiny_arb_prop (input logic clk_i, input logic rst_ni);

default clocking cb @(posedge clk_i); endclocking
default disable iff (!rst_ni);

//====DESIGNER-ADDED-SVA====//

a_first: assert property ((req_o == (|req_i)))
else begin
    $display("req_o wrong");
end

a_second: assert property ((gnt_i && req_o) |=> (gnt_o == $past(req_i)))
else begin
    $display("grant wrong");
end

// SVApshot: commented out due to unfixable syntax error in a_broken
// a_broken: assert property (bad syntax here)
// else begin
//     $display("never enabled");
// end

endmodule
"""


VCF_LOG = """\
Version X-2025.06-SP2 for linux64 - Jul 1, 2025
[Info] PROP_I_RESULT: FPV  tiny_arb_prop.a_first  checking  property  0:00:01
[Info] PROP_I_RESULT: FPV  tiny_arb_prop.a_first  bmc  proven  property  0:00:03
[Info] PROP_I_RESULT: FPV  tiny_arb_prop.a_first  bmc  non_vacuous  vacuity  0:00:03
[Info] PROP_I_RESULT: FPV  tiny_arb_prop.a_second  bmc  proven  property  0:00:04
[Info] PROP_I_RESULT: FPV  tiny_arb_prop.a_second  bmc  vacuous  vacuity  0:00:04
[Info] PROP_I_RESULT: FPV  tiny_arb_prop.a_third  bmc  falsified  property  0:00:05
[Info] PROP_I_RESULT: FPV  tiny_arb_prop.a_fourth  bmc  inconclusive  property  0:00:09
[Info] PROP_I_RESULT: FPV  tiny_arb.env_stable  bmc  proven  constraint  0:00:01
Peak Memory(MB) : 120
"""


class TestProofStatusParsing(unittest.TestCase):

    def setUp(self):
        self.result = proof_status.parse_vcformal_log(VCF_LOG)

    def test_only_properties_are_recorded(self):
        # The constraint line must not become a property; counting constraints
        # as proofs is exactly how a snapshot ends up overstating itself.
        self.assertNotIn('env_stable', self.result.records)
        self.assertEqual(len(self.result.records), 4)

    def test_five_way_qualification(self):
        qualifications = {
            name: record.qualification
            for name, record in self.result.records.items()
        }
        self.assertEqual(
            qualifications['a_first'], Qualification.PROVED_NON_VACUOUS)
        self.assertEqual(
            qualifications['a_second'], Qualification.PROVED_VACUOUS)
        self.assertEqual(
            qualifications['a_third'], Qualification.FAILING_PROPERTY_MISMATCH)
        self.assertEqual(
            qualifications['a_fourth'], Qualification.INCONCLUSIVE)

    def test_vacuous_proof_is_not_reported_as_proven_to_the_agent(self):
        legacy = self.result.legacy_results()
        self.assertEqual(legacy['a_first'], 'proven')
        self.assertEqual(legacy['a_second'], 'cex')

    def test_missing_assumption_changes_the_category(self):
        record = self.result.records['a_third']
        record.missing_assumptions = ['m_req_stable']
        self.assertEqual(
            record.qualification, Qualification.FAILING_MISSING_ASSUMPTION)

    def test_a_license_failure_is_not_reported_as_a_compile_error(self):
        log = '\n'.join([
            '                                   VC Static ',
            '               Version X-2025.06-SP2 for linux64',
            '*** Error: License Server, unable to checkout required license(s), '
            'please see license server log for details.',
            '** Error: Formal Shell Licensing. License checkout failed for '
            'license VC-FORMAL-TOKEN-SH',
        ])
        result = proof_status.parse_formal_log(log, 'vcformal', 'div_unit')
        self.assertTrue(result.summary.license_failure)
        self.assertFalse(result.summary.compile_failed)
        self.assertTrue(result.summary.no_per_property_results)

    def test_a_banner_prefixed_compile_error_is_still_a_compile_error(self):
        log = '*** Error: syntax error at div_unit_prop.sv line 42\n'
        result = proof_status.parse_formal_log(log, 'vcformal', 'div_unit')
        self.assertTrue(result.summary.compile_failed)
        self.assertFalse(result.summary.license_failure)

    def test_a_locked_vc_session_is_infrastructure_not_a_compile_failure(self):
        log = (
            '[Error]Another session is already running or previous run had some issues. '
            'Please check [vcst_rtdb/session.lock]\n'
            '[Error] (State:1) the VC Static Server exited.\n'
        )
        result = proof_status.parse_formal_log(log, 'vcformal', 'div_unit')
        self.assertTrue(result.summary.infrastructure_failure)
        self.assertFalse(result.summary.compile_failed)
        self.assertFalse(result.summary.license_failure)

    def test_missing_vcf_executable_is_infrastructure_failure(self):
        result = proof_status.parse_formal_log(
            './run_vcf_batch.sh: line 31: vcf: command not found\n',
            'vcformal',
            'div_unit',
        )
        self.assertTrue(result.summary.infrastructure_failure)
        self.assertFalse(result.summary.compile_failed)

    def test_vacuity_audit_separates_unchecked_proofs(self):
        audit = self.result.vacuity_audit()
        self.assertEqual(audit['proven_vacuity_non_vacuous'], 1)
        self.assertEqual(audit['proven_vacuity_vacuous'], 1)
        self.assertEqual(audit['proven_vacuity_not_checked'], 0)

    def test_assumption_dependence_audit(self):
        self.result.records['a_first'].assumption_dependence = ['m_req_stable']
        audit = self.result.assumption_dependence_audit()
        self.assertEqual(audit['proven_assumption_dependent'], 1)
        self.assertEqual(audit['proven_standalone'], 1)


DENSITY_REPORT = """\
DENSITY_SCOPE reg
Uncovered Registers (asserts+covers): 1
Uncovered Registers (asserts): 2
#1: tiny_arb.spare_q
#2: tiny_arb.debug_q
Uncovered Registers (covers): 0
Covered Registers (asserts+covers): 3
#1: tiny_arb.gnt_q
#2: tiny_arb.ptr_q
#3: tiny_arb.spare_q
Covered Registers (asserts): 2
#1: tiny_arb.gnt_q
#2: tiny_arb.ptr_q
Covered Registers (covers): 1
#1: tiny_arb.spare_q
Total Registers in given scope : 4
#1: tiny_arb.gnt_q
#2: tiny_arb.ptr_q
#3: tiny_arb.spare_q
#4: tiny_arb.debug_q
DENSITY_SCOPE pi
Uncovered Primary Inputs (asserts): 1
#1: tiny_arb.unused_i
Covered Primary Inputs (asserts): 3
#1: tiny_arb.req_i
#2: tiny_arb.gnt_i
#3: tiny_arb.clk_i
Total Primary Inputs in given scope : 4
#1: tiny_arb.req_i
#2: tiny_arb.gnt_i
#3: tiny_arb.clk_i
#4: tiny_arb.unused_i
"""


class TestToolReportedCoverage(unittest.TestCase):
    """The tool-side checker-coverage figures and their exact scope."""

    def setUp(self):
        self.scopes = proof_status.parse_assertion_density_report(DENSITY_REPORT)

    def test_both_scopes_are_parsed(self):
        self.assertEqual(set(self.scopes), {'reg', 'pi'})

    def test_covers_do_not_inflate_the_density(self):
        # spare_q is only in a cover's cone, so it must not count as covered:
        # a cover establishes reachability, not behaviour.
        registers = self.scopes['reg']
        self.assertEqual(registers.covered_by_asserts,
                         ['tiny_arb.gnt_q', 'tiny_arb.ptr_q'])
        self.assertEqual(registers.total_count, 4)
        self.assertAlmostEqual(registers.density, 0.5)

    def test_primary_input_scope(self):
        self.assertAlmostEqual(self.scopes['pi'].density, 0.75)

    def test_formal_core_coverage_uses_non_vacuous_proofs_only(self):
        result = proof_status.parse_vcformal_log(VCF_LOG)
        result.density = self.scopes
        # a_first is proved non-vacuously, a_second only vacuously: only the
        # former's core may contribute.
        # The density report writes vectors with a bit range and the formal core
        # report does not; the two must still match.
        result.records['a_first'].formal_core_register_names = ['tiny_arb.gnt_q[3:0]']
        result.records['a_second'].formal_core_register_names = ['tiny_arb.ptr_q']

        coverage = result.tool_checker_coverage()
        self.assertAlmostEqual(coverage['property_density'], 0.5)
        self.assertEqual(coverage['formal_core_coverage_count'], 1)
        self.assertAlmostEqual(coverage['formal_core_coverage'], 0.25)
        # The core is a subset of the COI, so this ordering must always hold.
        self.assertLessEqual(
            coverage['formal_core_coverage'], coverage['property_density'])

    def test_a_core_without_registers_is_called_out(self):
        # Proofs that need no register cannot be sensitive to state-holding
        # faults, however high the density is. Saying so is the whole point.
        result = proof_status.parse_vcformal_log(VCF_LOG)
        result.density = self.scopes
        result.records['a_first'].formal_core_input_names = ['tiny_arb.req_i']

        coverage = result.tool_checker_coverage()
        self.assertAlmostEqual(coverage['formal_core_coverage'], 0.0)
        self.assertIn('combinational reasoning', coverage['formal_core_note'])

    def test_metrics_carry_the_tool_figures(self):
        result = proof_status.parse_vcformal_log(VCF_LOG)
        result.density = self.scopes
        metrics = metrics_report.metrics_from_run('tiny_arb', result)
        self.assertAlmostEqual(
            metrics.fpv_checker_coverage['property_density'], 0.5)
        self.assertIn('property density (registers)', metrics.format_report())


class TestConeOfInfluence(unittest.TestCase):

    def setUp(self):
        self.graph = coi.build_signal_graph(SMALL_RTL, 'tiny_arb')

    def test_ports_are_classified(self):
        self.assertIn('req_i', self.graph.ports_in)
        self.assertIn('gnt_o', self.graph.ports_out)
        self.assertIn('gnt_q', self.graph.internals)

    def test_cone_follows_fan_in(self):
        cone = self.graph.cone_of_influence(['gnt_o'])
        self.assertIn('gnt_q', cone)
        self.assertIn('gnt_d', cone)
        self.assertIn('req_i', cone)

    def test_signals_declared_inside_generate_blocks_are_found(self):
        # Without this the cone stops at the generate boundary and the tool
        # reports a signal as unobserved that the FPV tool puts in the COI.
        self.assertIn('unused_slow', self.graph.internals)

    def test_a_generate_loop_body_declaration_is_found(self):
        rtl = """\
module gen_arb (input logic clk_i, input logic [3:0] req_i, output logic [3:0] gnt_o);
    for (genvar l = 0; l < 2; l++) begin : gen_level
        logic sel;
        assign sel = req_i[l*2+1];
        assign gnt_o[l*2] = req_i[l*2] & ~sel;
    end
endmodule
"""
        graph = coi.build_signal_graph(rtl, 'gen_arb')
        self.assertIn('sel', graph.internals)
        self.assertIn('sel', graph.cone_of_influence(['gnt_o']))

    def test_compiler_directives_are_not_taken_for_declarations(self):
        rtl = """\
module guarded (input logic clk_i, output logic q_o);
`ifndef VERILATOR
`ifndef XSIM
    logic real_signal;
`endif
`endif
    assign q_o = clk_i;
endmodule
"""
        graph = coi.build_signal_graph(rtl, 'guarded')
        self.assertIn('real_signal', graph.internals)
        self.assertNotIn('VERILATOR', graph.internals)
        self.assertNotIn('XSIM', graph.internals)

    def test_string_continuation_does_not_delete_code(self):
        # A string continued with a backslash used to swallow the rest of the
        # file, which silently emptied the fan-in graph.
        stripped = coi.strip_sv_noise(SMALL_RTL)
        self.assertIn('always_ff', stripped)
        self.assertIn('gnt_q', stripped)

    def test_diversity_of_two_disjoint_properties(self):
        assertions = [
            'a_one: assert property (req_o == |req_i) else begin $display("x"); end',
            'a_two: assert property (gnt_o == gnt_q) else begin $display("y"); end',
        ]
        cones = coi.compute_property_cones(assertions, self.graph)
        metrics = coi.compute_diversity(cones, self.graph)
        self.assertEqual(metrics.property_count, 2)
        self.assertEqual(metrics.distinct_cone_signatures, 2)
        self.assertGreater(metrics.mean_pairwise_jaccard_distance, 0.0)
        self.assertEqual(metrics.redundancy, 0.0)

    def test_identical_properties_are_reported_as_redundant(self):
        assertion = 'a_one: assert property (req_o == |req_i) else begin $display("x"); end'
        cones = coi.compute_property_cones([assertion, assertion], self.graph)
        metrics = coi.compute_diversity(cones, self.graph)
        self.assertEqual(metrics.distinct_cone_signatures, 1)
        self.assertEqual(metrics.redundancy, 0.5)


class TestMutationEngine(unittest.TestCase):

    def setUp(self):
        self.mutants = mutation.generate_mutants(
            SMALL_RTL, 'tiny_arb',
            mutation.MutationConfig(max_total=60, max_per_class=10))
        self.lines = SMALL_RTL.splitlines()

    def test_mutants_are_unique(self):
        fingerprints = [m.fingerprint for m in self.mutants]
        self.assertEqual(len(fingerprints), len(set(fingerprints)))

    def test_no_mutant_equals_the_original(self):
        original = mutation.fingerprint_source(SMALL_RTL)
        self.assertNotIn(original, {m.fingerprint for m in self.mutants})

    def test_several_fault_classes_are_covered(self):
        classes = {m.fault_class for m in self.mutants}
        self.assertIn(mutation.FaultClass.CONTROL, classes)
        self.assertIn(mutation.FaultClass.TIMING, classes)
        self.assertIn(mutation.FaultClass.RESET, classes)
        self.assertIn(mutation.FaultClass.STATE_UPDATE, classes)

    def test_generation_is_deterministic(self):
        again = mutation.generate_mutants(
            SMALL_RTL, 'tiny_arb',
            mutation.MutationConfig(max_total=60, max_per_class=10))
        self.assertEqual([m.mutant_id for m in self.mutants],
                         [m.mutant_id for m in again])

    def test_rtl_embedded_assertions_are_never_mutated(self):
        excluded = mutation.verification_regions(self.lines)
        assertion_line = next(
            index for index, line in enumerate(self.lines) if 'a_native' in line)
        self.assertTrue(excluded[assertion_line])
        self.assertFalse(any(m.line_number - 1 == assertion_line for m in self.mutants))

    def test_parameters_are_read_from_the_header(self):
        parameters = mutation.module_parameters(SMALL_RTL)
        self.assertEqual(parameters['NumIn'], 4)
        self.assertEqual(parameters['Fast'], 0)

    def test_branches_the_parameters_switch_off_are_skipped(self):
        dead = mutation.dead_generate_lines(
            self.lines, mutation.module_parameters(SMALL_RTL))
        fast_body = next(
            index for index, line in enumerate(self.lines)
            if 'assign gnt_d = req_i;' in line)
        slow_body = next(
            index for index, line in enumerate(self.lines)
            if 'unused_slow = 1' in line)
        self.assertTrue(dead[fast_body], 'Fast=0 branch must not be mutated')
        self.assertFalse(dead[slow_body], 'the taken branch must stay live')

    def test_a_condition_that_decides_the_same_way_is_not_a_mutant(self):
        # With NumIn = 4 both conditions are false, so the elaborated design is
        # unchanged and the mutant could never be detected.
        self.assertTrue(mutation.elaboration_equivalent(
            "    if (NumIn == unsigned'(1)) begin : gen_pass",
            "    if (NumIn == unsigned'(2)) begin : gen_pass",
            {'NumIn': 4}))
        self.assertFalse(mutation.elaboration_equivalent(
            "    if (NumIn == unsigned'(1)) begin : gen_pass",
            "    if (NumIn != unsigned'(1)) begin : gen_pass",
            {'NumIn': 4}))

    def test_a_run_time_condition_is_never_called_equivalent(self):
        self.assertFalse(mutation.elaboration_equivalent(
            '    if (req_o && gnt_i) begin',
            '    if (req_o || gnt_i) begin',
            {'NumIn': 4}))

    def test_a_write_only_register_makes_blocking_equivalent(self):
        # rr_q is never read inside the block, so the two assignment forms
        # elaborate to the same flop and the mutant is not a fault.
        lines = [
            'always_ff @(posedge clk_i or negedge rst_ni) begin : p_regs',
            '  if (!rst_ni) begin',
            "    rr_q <= '0;",
            '  end else begin',
            '    rr_q <= rr_d;',
            '  end',
            'end',
        ]
        extents = mutation.sequential_block_extents(lines)
        self.assertEqual(extents[2], (0, 6))
        self.assertTrue(mutation.scheduling_equivalent(lines, 2, extents[2]))
        self.assertTrue(mutation.scheduling_equivalent(lines, 4, extents[4]))

    def test_a_read_after_write_keeps_the_timing_mutant(self):
        lines = [
            'always_ff @(posedge clk_i) begin',
            '  count <= count_next;',
            '  total <= total + count;',
            'end',
        ]
        extents = mutation.sequential_block_extents(lines)
        self.assertFalse(mutation.scheduling_equivalent(lines, 1, extents[1]))

    def test_the_timing_class_drops_the_equivalent_rewrite(self):
        rtl = '\n'.join([
            'module holder (input logic clk_i, rst_ni, d_i, output logic q_o);',
            '  logic q;',
            '  always_ff @(posedge clk_i or negedge rst_ni) begin',
            '    if (!rst_ni) begin',
            "      q <= '0;",
            '    end else begin',
            '      q <= d_i;',
            '    end',
            '  end',
            '  assign q_o = q;',
            'endmodule',
        ])
        operators = {
            mutant.operator for mutant in mutation.generate_mutants(rtl, 'holder')
        }
        self.assertNotIn('nonblocking_to_blocking', operators)

    def test_trailing_comments_are_preserved(self):
        code, comment = mutation.split_trailing_comment('assign a = b; // note')
        self.assertEqual(code, 'assign a = b; ')
        self.assertEqual(comment, '// note')

    def test_function_assignments_are_not_declarations(self):
        self.assertFalse(coi.is_declaration('    do_add = a + b + c;'))
        self.assertFalse(coi.is_declaration('    next_pready           = pready;'))
        self.assertFalse(coi.is_declaration("    gnt_q <= '0;"))
        self.assertTrue(coi.is_declaration("    wire rst_n = arst_n ^ ARST_LVL;"))
        self.assertTrue(coi.is_declaration('    logic [3:0] opcode;'))

    def test_same_line_if_nba_is_not_rewritten_as_comparison(self):
        rtl = '\n'.join([
            'module fifo (input clk, rst, we, output [3:0] wp);',
            '  always @(posedge clk) begin',
            '    if(!rst) wp <= #1 4\'b0;',
            '    if (wp <= 4) overflow = 1\'b1;',
            '  end',
            'endmodule',
        ])
        mutants = mutation.generate_mutants(rtl, 'fifo')
        operators_on_nba = [
            m.operator for m in mutants if 'wp <= #1' in m.original_line
        ]
        self.assertNotIn('le_to_lt', operators_on_nba)
        self.assertFalse(any('wp < #1' in m.mutated_line for m in mutants))
        self.assertTrue(any(
            m.operator == 'le_to_lt' and 'wp < 4' in m.mutated_line
            for m in mutants
        ))

    def test_alu_core_mutates_operators_not_function_ports(self):
        rtl_path = os.path.join(
            os.path.dirname(__file__), '..',
            'benchmarks', '2605.06434', 'alu', 'alu_core.sv')
        with open(rtl_path, encoding='utf-8') as handle:
            rtl = handle.read()
        mutants = mutation.generate_mutants(
            rtl, 'alu_core',
            mutation.MutationConfig(max_total=30, max_per_class=5))
        self.assertGreater(len(mutants), 0)
        self.assertFalse(any(
            m.original_line.lstrip().startswith('input signed')
            for m in mutants
        ))
        datapath = [
            m for m in mutants if m.fault_class is mutation.FaultClass.DATAPATH
        ]
        self.assertTrue(datapath)
        self.assertTrue(any(
            m.operator in {
                'add_to_sub', 'sub_to_add', 'mul_to_div', 'div_to_mul',
                'bitand_to_bitor', 'bitor_to_bitand',
            }
            for m in datapath
        ))
        self.assertTrue(any(
            'a + b + c' in m.original_line or 'a - b - c' in m.original_line
            or 'a * b * c' in m.original_line
            for m in datapath
        ))
        self.assertFalse(any(
            'a + b + c + 1' in m.original_line for m in mutants
        ))

    def test_comment_module_does_not_expose_ansi_ports(self):
        rtl = '\n'.join([
            '// Use `module name` then one port per line',
            'module cipher (',
            '    output [63:0] data_o,',
            '    input [79:0] data_i',
            ');',
            '  assign data_o = data_i[63:0];',
            'endmodule',
        ])
        mutants = mutation.generate_mutants(rtl, 'cipher')
        self.assertFalse(any(
            'output [63:0] data_o' in m.original_line
            or 'input [79:0] data_i' in m.original_line
            for m in mutants
        ))

    def test_equivalent_mutants_leave_the_population(self):
        mutants = list(self.mutants[:3])
        mutants[0].equivalence = mutation.EquivalenceVerdict.EQUIVALENT
        for mutant in mutants[1:]:
            mutant.equivalence = mutation.EquivalenceVerdict.NON_EQUIVALENT
            mutant.detected_by = ['a_first']
        score = mutation.score_mutants(mutants)
        self.assertEqual(score.excluded_equivalent, 1)
        self.assertEqual(score.population, 2)
        self.assertEqual(score.detection_rate, 1.0)

    def test_unscreened_mutants_do_not_enter_the_denominator(self):
        mutants = list(self.mutants[:2])
        mutants[0].equivalence = mutation.EquivalenceVerdict.NOT_SCREENED
        mutants[1].equivalence = mutation.EquivalenceVerdict.UNKNOWN
        score = mutation.score_mutants(mutants)
        self.assertEqual(score.population, 0)
        self.assertEqual(score.excluded_equivalent, 2)


class TestAdaptiveStopping(unittest.TestCase):

    def policy(self, **overrides):
        config = adaptive_stop.AdaptiveStopConfig(**overrides)
        return adaptive_stop.StoppingPolicy(config)

    def test_first_attempt_is_always_taken(self):
        policy = self.policy()
        proceed, _ = policy.should_attempt(1, [])
        self.assertTrue(proceed)

    def test_hard_ceiling_is_respected(self):
        policy = self.policy(max_attempts_per_property=3)
        proceed, reason = policy.should_attempt(4, [])
        self.assertFalse(proceed)
        self.assertIs(reason, adaptive_stop.StopReason.MAX_ATTEMPTS)

    def test_repeated_fix_stops_the_repair(self):
        policy = self.policy()
        attempt = 'a_x: assert property (a |-> b) else begin $display("m"); end'
        repeat = 'a_x: assert property (a |-> b) else begin $display("other"); end'
        proceed, reason = policy.should_attempt(
            2, [attempt, repeat], latest_attempt=repeat)
        self.assertFalse(proceed)
        self.assertIs(reason, adaptive_stop.StopReason.NO_PROGRESS)

    def test_repeated_counterexample_stops_the_repair(self):
        policy = self.policy()
        proceed, reason = policy.should_attempt(
            2, ['a', 'b'],
            previous_counterexample='cex at 0:00:01 signal x=1',
            latest_counterexample='cex at 0:00:09 signal x=1')
        self.assertFalse(proceed)
        self.assertIs(reason, adaptive_stop.StopReason.REPEATED_COUNTEREXAMPLE)

    def test_repeated_failures_drive_the_estimate_below_the_floor(self):
        policy = self.policy(success_probability_floor=0.2)
        for _ in range(20):
            policy.record_attempt(2, succeeded=False)
        proceed, reason = policy.should_attempt(2, ['a', 'b'])
        self.assertFalse(proceed)
        self.assertIs(reason, adaptive_stop.StopReason.LOW_EXPECTED_VALUE)

    def test_global_attempt_budget_stops_everything(self):
        policy = self.policy(max_total_attempts=2)
        policy.record_attempt(1, succeeded=False)
        policy.record_attempt(1, succeeded=False)
        proceed, reason = policy.should_attempt(1, [])
        self.assertFalse(proceed)
        self.assertIs(reason, adaptive_stop.StopReason.GLOBAL_BUDGET_EXHAUSTED)

    def test_saved_attempts_are_reported(self):
        policy = self.policy(max_attempts_per_property=5)
        policy.record_attempt(1, succeeded=True)
        policy.finish_property(adaptive_stop.RepairOutcome(
            property_name='a_x', attempts=1, repaired=True,
            stop_reason=adaptive_stop.StopReason.REPAIRED))
        self.assertEqual(policy.to_dict()['attempts_saved_vs_fixed_budget'], 4)


class TestCostAccounting(unittest.TestCase):

    def test_price_is_per_million_tokens(self):
        ledger = llm_cost.CostLedger(model='gpt-4.1-mini')
        cost = ledger.record('generation', 1_000_000, 1_000_000)
        expected = (llm_cost.MODEL_PRICES['gpt-4.1-mini'].input_per_mtok
                    + llm_cost.MODEL_PRICES['gpt-4.1-mini'].output_per_mtok)
        self.assertAlmostEqual(cost, expected, places=6)
        self.assertEqual(ledger.currency, 'EUR')

    def test_stages_are_kept_apart(self):
        ledger = llm_cost.CostLedger(model='gpt-4.1-mini')
        ledger.record('syntax_repair', 100, 10)
        ledger.record('semantic_repair', 200, 20)
        ledger.record('semantic_repair', 200, 20)
        self.assertEqual(ledger.stages['semantic_repair'].calls, 2)
        self.assertEqual(ledger.total_calls, 3)
        self.assertEqual(ledger.total_input_tokens, 500)

    def test_estimated_calls_are_flagged(self):
        ledger = llm_cost.CostLedger(model='gpt-4.1-mini')
        ledger.record('generation', 10, 10, exact=False)
        ledger.record('generation', 10, 10, exact=True)
        self.assertEqual(ledger.estimated_usage_calls, 1)
        self.assertEqual(ledger.exact_usage_calls, 1)

    def test_exact_provider_counts_are_not_reported_as_estimates(self):
        ledger = llm_cost.CostLedger(model='gpt-5.3-codex')
        ledger.record('syntax_repair', 100, 50, exact=True)
        self.assertIn('provider_usage', ledger.accounting_used)
        self.assertNotIn('heuristic', ledger.format_summary())

        ledger.record('semantic_repair', 10, 5, exact=False)
        self.assertIn('mixed', ledger.accounting_used)

    def test_usage_is_read_from_both_api_shapes(self):
        class ChatUsage:
            prompt_tokens, completion_tokens = 11, 22

        class Response:
            usage = ChatUsage()

        self.assertEqual(
            llm_cost.extract_usage(Response()),
            {'input_tokens': 11, 'output_tokens': 22})


class TestMetrics(unittest.TestCase):

    def test_les_is_zero_when_nothing_is_detected(self):
        self.assertEqual(
            metrics_report.llm_efficiency_score(20, 0.0, 1.0), 0.0)

    def test_les_rewards_detection_per_euro(self):
        cheap = metrics_report.llm_efficiency_score(10, 0.5, 0.10)
        expensive = metrics_report.llm_efficiency_score(10, 0.5, 1.00)
        self.assertGreater(cheap, expensive)

    def test_cost_floor_keeps_the_score_finite(self):
        score = metrics_report.llm_efficiency_score(4, 1.0, 0.0, cost_floor=0.01)
        self.assertEqual(score, 400.0)

    def test_snapshot_yield_counts_only_non_vacuous_proofs(self):
        qualification = metrics_report.QualificationMetrics(
            total_properties=10, proved_non_vacuous=4, proved_vacuous=3,
            failing_property_mismatch=3)
        self.assertEqual(qualification.snapshot_worthy, 4)
        self.assertAlmostEqual(qualification.snapshot_yield, 0.4)

    def test_detection_rate_by_class(self):
        report = metrics_report.SensitivityReport(
            mutants_applied=4, mutants_detected=3,
            detection_by_class={'control': {'applied': 2, 'detected': 2},
                                'timing': {'applied': 2, 'detected': 1}})
        self.assertAlmostEqual(report.detection_rate, 0.75)
        self.assertAlmostEqual(report.class_rates()['timing'], 0.5)


class TestContractExtraction(unittest.TestCase):

    def test_enabled_assertions_are_found(self):
        names = formal_harness.contract_names(PROPERTY_FILE)
        self.assertEqual(names, ['a_first', 'a_second'])

    def test_non_contract_assertions_are_commented_out(self):
        filtered = formal_harness.contract_property_file(PROPERTY_FILE, ['a_first'])
        self.assertEqual(formal_harness.contract_names(filtered), ['a_first'])
        self.assertIn('a_first: assert property', filtered)
        self.assertIn('// [not in contract] a_second: assert property', filtered)

    def test_the_checker_shell_is_left_intact(self):
        filtered = formal_harness.contract_property_file(PROPERTY_FILE, [])
        self.assertIn('default clocking cb', filtered)
        self.assertIn('default disable iff', filtered)
        self.assertTrue(filtered.rstrip().endswith('endmodule'))


class TestMutantClassification(unittest.TestCase):

    def result(self, statuses):
        result = proof_status.FormalRunResult()
        for name, (proof, vacuity) in statuses.items():
            result.records[name] = proof_status.PropertyRecord(
                name=name, proof_status=proof, vacuity_status=vacuity)
        return result

    def test_falsified_contract_property_means_detected(self):
        reference = self.result({
            'a_first': (ProofStatus.PROVEN, VacuityStatus.NON_VACUOUS)})
        mutant = self.result({
            'a_first': (ProofStatus.FALSIFIED, VacuityStatus.NOT_CHECKED)})
        outcome = evaluate.classify_mutant_run(['a_first'], reference, mutant)
        self.assertEqual(outcome.detected_by, ['a_first'])

    def test_inconclusive_is_weakened_not_detected(self):
        reference = self.result({
            'a_first': (ProofStatus.PROVEN, VacuityStatus.NON_VACUOUS)})
        mutant = self.result({
            'a_first': (ProofStatus.INCONCLUSIVE, VacuityStatus.NOT_CHECKED)})
        outcome = evaluate.classify_mutant_run(['a_first'], reference, mutant)
        self.assertEqual(outcome.detected_by, [])
        self.assertEqual(outcome.weakened, ['a_first'])

    def test_properties_that_never_held_cannot_detect(self):
        reference = self.result({
            'a_weak': (ProofStatus.PROVEN, VacuityStatus.VACUOUS)})
        mutant = self.result({
            'a_weak': (ProofStatus.FALSIFIED, VacuityStatus.NOT_CHECKED)})
        outcome = evaluate.classify_mutant_run(['a_weak'], reference, mutant)
        self.assertEqual(outcome.detected_by, [])


class TestEscapeAnalysis(unittest.TestCase):

    def test_a_mutation_outside_every_cone_is_reported_as_such(self):
        analyser = evaluate.EscapeAnalyser(
            SMALL_RTL, 'tiny_arb',
            ['a_one: assert property (req_o == |req_i) else begin $display("x"); end'])
        mutant = mutation.Mutant(
            mutant_id='m1', fault_class=mutation.FaultClass.RESET,
            operator='reset_value_ones', line_number=1,
            original_line="gnt_q <= '0;", mutated_line="gnt_q <= '1;")
        reason = analyser.explain(mutant, evaluate.MutantOutcome(mutant_id='m1'))
        self.assertIn('outside every property cone', reason)

    def test_a_mutation_inside_a_cone_names_the_watching_properties(self):
        analyser = evaluate.EscapeAnalyser(
            SMALL_RTL, 'tiny_arb',
            ['a_two: assert property (gnt_o == gnt_q) else begin $display("y"); end'])
        mutant = mutation.Mutant(
            mutant_id='m2', fault_class=mutation.FaultClass.STATE_UPDATE,
            operator='state_held', line_number=1,
            original_line='gnt_q <= gnt_d;', mutated_line='gnt_q <= gnt_q;')
        reason = analyser.explain(mutant, evaluate.MutantOutcome(mutant_id='m2'))
        self.assertIn('a_two', reason)


class TestAssumptionScreening(unittest.TestCase):
    """The screens that decide whether a generated assumption may be kept."""

    def test_an_assumption_that_hides_a_mutant_is_rejected(self):
        verdict, masked = assumption_gen.screen_mutation_sensitivity(
            detected_before=['m_control_001', 'm_reset_002'],
            detected_after=['m_control_001'])
        self.assertEqual(verdict, assumption_gen.ScreenVerdict.FAIL)
        self.assertEqual(masked, ['m_reset_002'])

    def test_an_assumption_that_only_adds_detections_passes(self):
        verdict, masked = assumption_gen.screen_mutation_sensitivity(
            detected_before=['m_control_001'],
            detected_after=['m_control_001', 'm_timing_003'])
        self.assertEqual(verdict, assumption_gen.ScreenVerdict.PASS)
        self.assertEqual(masked, [])

    def test_an_assumption_that_makes_a_proof_vacuous_is_rejected(self):
        baseline = proof_status.parse_vcformal_log(VCF_LOG)
        candidate = proof_status.parse_vcformal_log(VCF_LOG)
        candidate.records['a_first'].vacuity_status = VacuityStatus.VACUOUS
        verdict, newly_vacuous = assumption_gen.screen_reachability_preservation(
            baseline, candidate)
        self.assertEqual(verdict, assumption_gen.ScreenVerdict.FAIL)
        self.assertEqual(newly_vacuous, ['a_first'])

    def test_the_screen_report_names_the_masked_mutants(self):
        result = evaluate.AssumptionScreenResult(
            detected_without=['m_reset_002'], detected_with=[],
            masked=['m_reset_002'], verdict='fail')
        report = result.format_report()
        self.assertIn('m_reset_002', report)
        self.assertIn('FAIL', report)

    def test_injecting_twice_does_not_declare_the_assumption_twice(self):
        text = 'module m_prop;\na_x: assert property (1);\nendmodule\n'
        block = "m_env: assume property (flush |-> !valid);\n"
        once = assumption_gen.inject_assumptions(text, block)
        twice = assumption_gen.inject_assumptions(once, block)
        self.assertEqual(twice.count('m_env:'), 1)
        self.assertEqual(once, twice)

    def test_assumption_parser_reads_structured_blocks(self):
        candidates = assumption_gen.parse_assumption_response(
            '---\n'
            'id: req_stable_until_grant\n'
            'property: req_i && !gnt_i |=> req_i\n'
            'rationale: Hold request until grant\n'
            'targets: a_addr\n'
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].expression, 'req_i && !gnt_i |=> req_i')
        self.assertFalse(assumption_gen.assumption_decision_is_none(
            '---\nid: req_stable_until_grant\nproperty: req_i |=> req_i\n'))

    def test_empty_assumption_reply_is_not_an_explicit_none(self):
        self.assertFalse(assumption_gen.assumption_decision_is_none(''))
        self.assertFalse(assumption_gen.assumption_decision_is_none('   '))
        self.assertFalse(assumption_gen.assumption_reply_usable(''))

    def test_explicit_none_assumption_decision_is_usable(self):
        reply = (
            '---\n'
            'decision: none\n'
            'rationale: The CEX drives legal handshake inputs\n'
        )
        self.assertTrue(assumption_gen.assumption_decision_is_none(reply))
        self.assertTrue(assumption_gen.assumption_reply_usable(reply))
        self.assertEqual(assumption_gen.parse_assumption_response(reply), [])

    def test_assumption_prompt_forbids_an_empty_reply(self):
        prompt = assumption_gen.build_assumption_prompt(
            'tiny', 'module tiny; endmodule',
            [('a_x', 'a_x: assert property (1);')])
        self.assertIn('decision: none', prompt)
        self.assertNotIn('or nothing at all', prompt)
        self.assertIn('Never reply with an empty message', prompt)

    def test_an_injected_assumption_block_can_be_stripped_again(self):
        candidate = assumption_gen.AssumptionCandidate(
            name='m_req_stable', expression='req_i |=> req_i')
        injected = assumption_gen.inject_assumptions(
            PROPERTY_FILE, candidate.to_sv())
        self.assertIn('m_req_stable', injected)
        # The screen compares the file with and without the block, so the
        # round trip has to be exact.
        self.assertEqual(
            assumption_gen.strip_assumptions(injected).strip(),
            PROPERTY_FILE.strip())


class TestBaselineHarness(unittest.TestCase):
    """Every method must be measured against the same checker shell."""

    def test_the_shell_is_shared_and_the_assertions_are_not(self):
        shell = baselines.checker_shell(PROPERTY_FILE)
        self.assertIn('default clocking cb', shell)
        self.assertNotIn('a_first: assert property', shell)

    def test_a_property_set_is_assembled_into_that_shell(self):
        shell = baselines.checker_shell(PROPERTY_FILE)
        assembled = baselines.assemble_property_file(
            shell,
            ['a_new: assert property (req_o) else begin $display("x"); end'])
        self.assertIn('default clocking cb', assembled)
        self.assertEqual(formal_harness.contract_names(assembled), ['a_new'])
        self.assertTrue(assembled.rstrip().endswith('endmodule'))

    def test_a_non_elaborating_set_is_reported_not_hidden(self):
        metrics = metrics_report.metrics_from_run('tiny_arb', None, method='one_shot')
        property_sets = {
            'one_shot': baselines.PropertySet(
                method='one_shot', compile_failed=True,
                unavailable_reason='does not elaborate'),
        }
        table = baselines.format_comparison(
            'tiny_arb', {'one_shot': metrics}, property_sets)
        self.assertIn('does not elaborate', table)
        # Nothing proved and nothing detected must score zero, not divide badly.
        self.assertEqual(metrics.les, 0.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
