#!/usr/bin/env python3
"""Contract tests for parent-instantiation parameter guidance.

Direct submodules inherit the parent's #() overrides through the bind. Without
telling the model those values, it invents both polarities of config-gated
properties and the ones that never fire prove vacuously. These tests pin the
extraction, the shared-vs-differing aggregation, and the prompt text the flow
injects.

    python3 tests/test_instantiation_params.py  # 25 tests
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import _path_setup  # noqa: F401

import instantiation_params as ip
import main


PARENT_WITH_TWO_ARBITERS = '''
module fpnew_top #(
  parameter int unsigned NUM_OPGROUPS = 4
) (
  input logic clk_i
);
  // comment with rr_arb_tree should not invent an instance
  rr_arb_tree #(
    .NumIn     ( NUM_OPGROUPS ),
    .DataType  ( output_t     ),
    .AxiVldRdy ( 1'b1         )
  ) i_arbiter (
    .clk_i (clk_i)
  );

  rr_arb_tree #(
    .NumIn     ( NUM_FORMATS ),
    .AxiVldRdy ( 1'b1        )
  ) i_fmt_arb (
    .clk_i (clk_i)
  );

  other_mod u_other (.clk_i(clk_i));
endmodule
'''

LEAF_RR_ARB = '''
module rr_arb_tree #(
  parameter int unsigned NumIn     = 64,
  parameter type         DataType  = logic [31:0],
  parameter bit          ExtPrio   = 1'b0,
  parameter bit          AxiVldRdy = 1'b0,
  parameter bit          LockIn    = 1'b0,
  parameter bit          FairArb   = 1'b1
) (
  input logic clk_i
);
endmodule
'''

PARENT_WITH_SAME_OVERRIDES = '''
module parent (input logic clk_i);
  child #(
    .Width (8),
    .Enable (1'b1)
  ) u0 (.clk_i(clk_i));
  child #(
    .Width (8),
    .Enable (1'b1)
  ) u1 (.clk_i(clk_i));
endmodule
'''

LEAF_CHILD = '''
module child #(
  parameter int Width = 4,
  parameter bit Enable = 1'b0,
  parameter bit Mode = 1'b0
) (input logic clk_i);
endmodule
'''


class TestNamedOverrideParsing(unittest.TestCase):

    def test_named_overrides_survive_nested_parentheses(self):
        block = ".NumIn(NUM_OPGROUPS), .DataType(logic [7:0]), .AxiVldRdy(1'b1)"
        self.assertEqual(
            ip.parse_named_overrides(block),
            {
                'NumIn': 'NUM_OPGROUPS',
                'DataType': 'logic [7:0]',
                'AxiVldRdy': "1'b1",
            },
        )

    def test_an_empty_parameter_list_is_no_overrides(self):
        self.assertEqual(ip.parse_named_overrides(''), {})
        self.assertEqual(ip.parse_named_overrides(None), {})


class TestDirectInstanceExtraction(unittest.TestCase):

    def test_only_direct_instances_are_collected(self):
        instances = ip.extract_direct_instances(PARENT_WITH_TWO_ARBITERS)
        by_name = {(i.module, i.instance): dict(i.overrides) for i in instances}
        self.assertIn(('rr_arb_tree', 'i_arbiter'), by_name)
        self.assertIn(('rr_arb_tree', 'i_fmt_arb'), by_name)
        self.assertIn(('other_mod', 'u_other'), by_name)
        self.assertEqual(by_name[('rr_arb_tree', 'i_arbiter')]['AxiVldRdy'], "1'b1")
        self.assertEqual(by_name[('other_mod', 'u_other')], {})

    def test_comments_do_not_invent_instances(self):
        text = '''
module top;
  // rr_arb_tree #( .AxiVldRdy(1'b0) ) ghost (.clk_i);
  real_mod u_real (.clk_i);
endmodule
'''
        modules = {i.module for i in ip.extract_direct_instances(text)}
        self.assertEqual(modules, {'real_mod'})

    def test_keywords_are_not_modules(self):
        text = '''
module top;
  always_ff @(posedge clk_i) begin
  end
  case (state)
    0: next = 1;
  endcase
  child u_child (.clk_i);
endmodule
'''
        modules = {i.module for i in ip.extract_direct_instances(text)}
        self.assertEqual(modules, {'child'})


class TestLeafDefaults(unittest.TestCase):

    def test_header_defaults_are_read(self):
        defaults = ip.extract_module_parameter_defaults(LEAF_RR_ARB)
        self.assertEqual(defaults['ExtPrio'], "1'b0")
        self.assertEqual(defaults['AxiVldRdy'], "1'b0")
        self.assertEqual(defaults['NumIn'], '64')


class TestAggregation(unittest.TestCase):

    def test_shared_and_differing_overrides_are_separated(self):
        instances = ip.extract_direct_instances(PARENT_WITH_TWO_ARBITERS)
        defaults = ip.extract_module_parameter_defaults(LEAF_RR_ARB)
        context = ip.aggregate_module_context('rr_arb_tree', instances, defaults)

        self.assertEqual(context.shared_overrides['AxiVldRdy'], "1'b1")
        self.assertIn('NumIn', context.differing_overrides)
        self.assertEqual(
            [pair[1] for pair in context.differing_overrides['NumIn']],
            ['NUM_OPGROUPS', 'NUM_FORMATS'],
        )
        self.assertEqual(context.untouched_defaults['ExtPrio'], "1'b0")
        self.assertEqual(context.untouched_defaults['LockIn'], "1'b0")
        self.assertNotIn('AxiVldRdy', context.untouched_defaults)

    def test_identical_instances_share_every_override(self):
        instances = ip.extract_direct_instances(PARENT_WITH_SAME_OVERRIDES)
        defaults = ip.extract_module_parameter_defaults(LEAF_CHILD)
        context = ip.aggregate_module_context('child', instances, defaults)
        self.assertEqual(context.shared_overrides, {'Width': '8', 'Enable': "1'b1"})
        self.assertEqual(context.differing_overrides, {})
        self.assertEqual(context.untouched_defaults['Mode'], "1'b0")


    def test_passthrough_overrides_are_not_fixed_polarities(self):
        parent = """
module parent (input logic clk_i);
  child #(.Width(WIDTH), .Enable(1'b1)) u0 (.clk_i(clk_i));
endmodule
"""
        instances = ip.extract_direct_instances(parent)
        defaults = ip.extract_module_parameter_defaults(LEAF_CHILD)
        context = ip.aggregate_module_context('child', instances, defaults)
        self.assertEqual(context.passthrough_overrides, {'Width': 'WIDTH'})
        self.assertEqual(context.shared_overrides, {'Enable': "1'b1"})
        self.assertNotIn('Width', context.shared_overrides)
        text = ip.format_instantiation_prompt(context)
        self.assertIn('parent expression', text)
        self.assertIn('Width = WIDTH', text)

    def test_a_partial_override_is_not_treated_as_shared(self):
        parent = '''
module parent (input logic clk_i);
  child #(.Enable(1'b1), .Mode(1'b1)) u0 (.clk_i(clk_i));
  child #(.Enable(1'b1)) u1 (.clk_i(clk_i));
endmodule
'''
        instances = ip.extract_direct_instances(parent)
        defaults = ip.extract_module_parameter_defaults(LEAF_CHILD)
        context = ip.aggregate_module_context('child', instances, defaults)
        self.assertEqual(context.shared_overrides['Enable'], "1'b1")
        self.assertIn('Mode', context.differing_overrides)
        self.assertNotIn('Mode', context.untouched_defaults)


class TestPromptFormatting(unittest.TestCase):

    def test_prompt_names_shared_differing_and_defaults(self):
        instances = ip.extract_direct_instances(PARENT_WITH_TWO_ARBITERS)
        defaults = ip.extract_module_parameter_defaults(LEAF_RR_ARB)
        context = ip.aggregate_module_context('rr_arb_tree', instances, defaults)
        text = ip.format_instantiation_prompt(context)

        self.assertIn('PARENT INSTANTIATION CONTEXT for rr_arb_tree', text)
        self.assertIn("AxiVldRdy = 1'b1", text)
        self.assertIn('opposite polarity', text)
        self.assertIn('NumIn:', text)
        self.assertIn('parametric', text)
        self.assertIn("ExtPrio = 1'b0", text)
        self.assertIn('i_arbiter:', text)
        self.assertIn('i_fmt_arb:', text)

    def test_empty_context_produces_no_prompt(self):
        context = ip.ModuleInstantiationContext(module='orphan')
        self.assertEqual(ip.format_instantiation_prompt(context), '')


class TestContextFileRoundTrip(unittest.TestCase):

    def test_context_is_written_where_the_agent_looks(self):
        instances = ip.extract_direct_instances(PARENT_WITH_SAME_OVERRIDES)
        defaults = ip.extract_module_parameter_defaults(LEAF_CHILD)
        context = ip.aggregate_module_context('child', instances, defaults)
        with tempfile.TemporaryDirectory() as tmp:
            path = ip.write_context_file(context, cwd=tmp)
            self.assertTrue(path.endswith(
                os.path.join('ft_child', 'sva', 'instantiation_context.txt')))
            loaded = ip.read_context_file('child', cwd=tmp)
            self.assertIn("Enable = 1'b1", loaded)
            self.assertIn("Mode = 1'b0", loaded)


class TestInitialGenerationPromptIncludesContext(unittest.TestCase):

    def test_context_is_appended_before_the_rtl(self):
        captured = {}

        class FakeCompletions:
            def create(self, **kwargs):
                captured['prompt'] = kwargs['messages'][0]['content']

                class Choice:
                    message = type('M', (), {
                        'content': (
                            '---\nid: req_out\n'
                            'property: |req_i |-> req_o\n'
                            'failure: request lost\n'
                        )
                    })()

                class Completion:
                    choices = [Choice()]
                    usage = None

                return Completion()

        class FakeChat:
            completions = FakeCompletions()

        class FakeClient:
            chat = FakeChat()

        with tempfile.TemporaryDirectory() as tmp:
            rtl = os.path.join(tmp, 'rr_arb_tree.sv')
            with open(rtl, 'w', encoding='utf-8') as handle:
                handle.write(LEAF_RR_ARB)
            context = ip.format_instantiation_prompt(
                ip.aggregate_module_context(
                    'rr_arb_tree',
                    ip.extract_direct_instances(PARENT_WITH_TWO_ARBITERS),
                    ip.extract_module_parameter_defaults(LEAF_RR_ARB),
                )
            )
            with mock.patch.dict(os.environ, {
                'OPENAI_API_KEY': 'test-key',
                'SVAPSHOT_INITIAL_ASSERTIONS_PATH': os.path.join(
                    tmp, 'initial_assertions'),
            }), mock.patch.object(main, 'OpenAI', return_value=FakeClient()), \
                    mock.patch('main.extract_usage', return_value=None), \
                    mock.patch('main._model_config', return_value=None), \
                    mock.patch('main.os.getcwd', return_value=tmp):
                ok = main.run_initial_assertion_generation(
                    rtl, 'gpt-4o-mini', instantiation_context=context)

        self.assertTrue(ok)
        prompt = captured['prompt']
        self.assertIn('PARENT INSTANTIATION CONTEXT for rr_arb_tree', prompt)
        self.assertIn("AxiVldRdy = 1'b1", prompt)
        self.assertLess(
            prompt.index('PARENT INSTANTIATION CONTEXT'),
            prompt.index('RTL MODULE:'),
        )


PARENT_WITH_FEATURES = '''
module parent #(
  parameter pkg::features_t Features = pkg::WIDE,
  parameter int unsigned TrueSIMDClass = 0
) (
  input logic clk_i
);
  localparam int unsigned WIDTH = Features.Width;
  localparam logic EnableVectors = Features.EnableVectors;
  localparam pkg::fmt_t FpFmtMask = Features.FpFmtMask;

  for (genvar opgrp = 0; opgrp < 4; opgrp++) begin : gen_groups
    localparam pkg::opgroup_e OpGroup = pkg::opgroup_e'(opgrp);
    child #(
      .Width (WIDTH),
      .EnableVectors (EnableVectors),
      .FpFmtMask (FpFmtMask),
      .OpGroup (OpGroup),
      .TrueSIMDClass (TrueSIMDClass)
    ) i_child (.clk_i(clk_i));
  end
endmodule
'''

PACKAGE_FEATURES = '''
package pkg;
  typedef struct packed {
    int unsigned Width;
    logic EnableVectors;
    logic [4:0] FpFmtMask;
  } features_t;
  typedef enum logic [1:0] { ADDMUL, DIVSQRT } opgroup_e;
  typedef logic [4:0] fmt_t;
  localparam features_t WIDE = '{
    Width:         64,
    EnableVectors: 1'b1,
    FpFmtMask:     5'b11111
  };
endpackage
'''

LEAF_FEATURES = '''
module child #(
  parameter int unsigned Width = 32,
  parameter logic EnableVectors = 1'b1,
  parameter logic [4:0] FpFmtMask = 5'b11111,
  parameter pkg::opgroup_e OpGroup = pkg::ADDMUL,
  parameter int unsigned TrueSIMDClass = 0
) (input logic clk_i);
endmodule
'''


class TestConstantResolution(unittest.TestCase):

    def test_literals_and_struct_fields_fold(self):
        parent_env, package_envs, imported = ip.build_resolution_env(
            PARENT_WITH_FEATURES, {'pkg': PACKAGE_FEATURES})
        self.assertEqual(
            ip.resolve_constant_expr(
                'WIDTH', parent_env, package_envs, imported),
            '64',
        )
        self.assertEqual(
            ip.resolve_constant_expr(
                'EnableVectors', parent_env, package_envs, imported),
            "1'b1",
        )
        self.assertEqual(
            ip.resolve_constant_expr(
                'FpFmtMask', parent_env, package_envs, imported),
            "5'b11111",
        )
        self.assertEqual(
            ip.resolve_constant_expr(
                'TrueSIMDClass', parent_env, package_envs, imported),
            '0',
        )

    def test_a_genvar_cast_does_not_fold(self):
        parent_env, package_envs, imported = ip.build_resolution_env(
            PARENT_WITH_FEATURES, {'pkg': PACKAGE_FEATURES})
        self.assertIsNone(ip.resolve_constant_expr(
            "pkg::opgroup_e'(opgrp)", parent_env, package_envs, imported))

    def test_a_function_call_does_not_fold(self):
        parent_env, package_envs, imported = ip.build_resolution_env(
            'module p; localparam int unsigned N = pkg::num_lanes(32); endmodule',
            {'pkg': 'package pkg; endpackage'})
        self.assertIsNone(ip.resolve_constant_expr(
            'N', parent_env, package_envs, imported))


class TestResolvedElaborationOverrides(unittest.TestCase):

    def test_shared_parent_constants_are_applied_and_genvars_are_not(self):
        instances = ip.extract_direct_instances(PARENT_WITH_FEATURES)
        defaults = ip.extract_module_parameter_defaults(LEAF_FEATURES)
        context = ip.aggregate_module_context('child', instances, defaults)
        resolved = ip.resolve_elaboration_overrides(
            context, PARENT_WITH_FEATURES, {'pkg': PACKAGE_FEATURES})
        self.assertEqual(resolved['Width'], '64')
        self.assertEqual(resolved['EnableVectors'], "1'b1")
        self.assertEqual(resolved['FpFmtMask'], "5'b11111")
        self.assertEqual(resolved['TrueSIMDClass'], '0')
        self.assertNotIn('OpGroup', resolved)

    def test_an_unresolved_passthrough_is_not_invented(self):
        parent = '''
module parent (input logic clk_i);
  child #(.Width(WIDTH)) u0 (.clk_i(clk_i));
endmodule
'''
        instances = ip.extract_direct_instances(parent)
        context = ip.aggregate_module_context(
            'child', instances, ip.extract_module_parameter_defaults(LEAF_CHILD))
        self.assertEqual(context.passthrough_overrides['Width'], 'WIDTH')
        self.assertEqual(
            ip.resolve_elaboration_overrides(context, parent, {}),
            {},
        )

    def test_identical_literals_are_elaboratable(self):
        instances = ip.extract_direct_instances(PARENT_WITH_SAME_OVERRIDES)
        context = ip.aggregate_module_context(
            'child', instances, ip.extract_module_parameter_defaults(LEAF_CHILD))
        self.assertEqual(
            ip.resolve_elaboration_overrides(context, PARENT_WITH_SAME_OVERRIDES),
            {'Width': '8', 'Enable': "1'b1"},
        )

    def test_prompt_names_the_resolved_elaborate_constants(self):
        instances = ip.extract_direct_instances(PARENT_WITH_FEATURES)
        context = ip.aggregate_module_context(
            'child', instances, ip.extract_module_parameter_defaults(LEAF_FEATURES))
        context.resolved_elaboration_overrides = ip.resolve_elaboration_overrides(
            context, PARENT_WITH_FEATURES, {'pkg': PACKAGE_FEATURES})
        text = ip.format_instantiation_prompt(context)
        self.assertIn('FPV elaborates this leaf', text)
        self.assertIn('Width = 64', text)
        self.assertNotIn('OpGroup = ', text.split('FPV elaborates')[1])

    def test_pvalue_flags_skip_unsafe_values(self):
        flags = ip.format_vcs_pvalue_string('child', {
            'Width': '64',
            'EnableVectors': "1'b1",
            'Bad': '1 0',
            'Also': '{default:0}',
        })
        self.assertEqual(
            flags,
            '-pvalue+child.EnableVectors=1 -pvalue+child.Width=64',
        )

    def test_based_literals_become_decimal_pvalues(self):
        # VC Formal launches VCS with sh -c; a raw 5'b11111 breaks the quote.
        flags = ip.format_vcs_pvalue_string('child', {
            'EnableVectors': "1'b1",
            'FpFmtMask': "5'b11111",
            'IntFmtMask': "4'b1111",
            'Hex': "8'hFF",
            'Xstate': "4'bxx00",
        })
        self.assertEqual(
            flags,
            '-pvalue+child.EnableVectors=1 '
            '-pvalue+child.FpFmtMask=31 '
            '-pvalue+child.Hex=255 '
            '-pvalue+child.IntFmtMask=15',
        )
        self.assertNotIn("'", flags)

    def test_elaboration_overrides_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = ip.write_elaboration_overrides(
                'child', {'Width': '64'}, cwd=tmp)
            self.assertTrue(path.endswith(os.path.join(
                'ft_child', 'sva', 'elaboration_parameters.json')))
            self.assertEqual(
                ip.read_elaboration_overrides('child', cwd=tmp),
                {'Width': '64'},
            )


class TestFpnewTopParentConstants(unittest.TestCase):
    """Lock the UCTLAB case: parent Width=64, not the leaf default 32."""

    def test_fpnew_top_resolves_width_and_not_opgroup(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        top = os.path.join(
            repo, 'benchmarks', 'sargantana', 'rtl', 'datapath', 'rtl',
            'exe_stage', 'rtl', 'fpu', 'src', 'fpnew_top.sv')
        pkg = os.path.join(os.path.dirname(top), 'fpnew_pkg.sv')
        leaf = os.path.join(os.path.dirname(top), 'fpnew_opgroup_block.sv')
        if not (os.path.isfile(top) and os.path.isfile(pkg) and os.path.isfile(leaf)):
            self.skipTest('fpnew sources are not in this checkout')
        with open(pkg, encoding='utf-8', errors='replace') as handle:
            package_text = handle.read()
        contexts = ip.build_contexts_for_parent(
            top,
            {'fpnew_opgroup_block': leaf},
            {'fpnew_pkg': package_text},
        )
        resolved = contexts['fpnew_opgroup_block'].resolved_elaboration_overrides
        self.assertEqual(resolved.get('Width'), '64')
        self.assertEqual(resolved.get('EnableVectors'), "1'b1")
        self.assertEqual(resolved.get('FpFmtMask'), "5'b11111")
        self.assertNotIn('OpGroup', resolved)
        self.assertNotIn('FmtUnitTypes', resolved)


class TestMmuTopParentForTlbPtw(unittest.TestCase):
    """Sargantana mmu_top instantiates tlb/ptw with empty #(); package config."""

    @classmethod
    def setUpClass(cls):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.parent = os.path.join(
            repo, 'benchmarks', 'sargantana', 'rtl', 'mmu', 'rtl', 'mmu_top.sv')
        cls.tlb = os.path.join(
            repo, 'benchmarks', 'sargantana', 'rtl', 'mmu', 'rtl', 'tlb',
            'tlb.sv')
        cls.ptw = os.path.join(
            repo, 'benchmarks', 'sargantana', 'rtl', 'mmu', 'rtl', 'ptw',
            'ptw.sv')
        cls.pkg = os.path.join(
            repo, 'benchmarks', 'sargantana', 'rtl', 'mmu', 'includes',
            'mmu_pkg.sv')
        if not all(os.path.isfile(path) for path in (
            cls.parent, cls.tlb, cls.ptw, cls.pkg,
        )):
            raise unittest.SkipTest('Sargantana MMU sources are not in this checkout')

    def test_mmu_top_names_tlb_and_ptw_instances(self):
        contexts = ip.build_contexts_for_parent(
            self.parent,
            {'tlb': self.tlb, 'ptw': self.ptw},
        )
        self.assertIn('tlb', contexts)
        self.assertIn('ptw', contexts)
        self.assertEqual(
            {inst.instance for inst in contexts['tlb'].instances},
            {'itlb', 'dtlb'},
        )
        self.assertEqual(
            [inst.instance for inst in contexts['ptw'].instances],
            ['ptw_inst'],
        )
        prompt = ip.format_instantiation_prompt(contexts['tlb'])
        self.assertIn('PARENT INSTANTIATION CONTEXT for tlb', prompt)
        self.assertIn('itlb:', prompt)
        self.assertIn('no #() overrides', prompt)

    def test_leaf_as_top_reads_parent_rtl_flag(self):
        prompt, overrides = main.apply_external_parent_context(
            self.parent, self.tlb, ['sources'], 'packages')
        self.assertIn('PARENT INSTANTIATION CONTEXT for tlb', prompt)
        self.assertIn('dtlb:', prompt)
        self.assertEqual(overrides, {})

    def test_sibling_library_dirs_include_common_plru(self):
        dirs = main.extra_library_dirs(self.tlb)
        self.assertTrue(any(path.endswith(os.path.join('rtl', 'common')) for path in dirs))
        self.assertTrue(any(os.path.isfile(os.path.join(path, 'pseudoLRU.sv')) for path in dirs))

    def test_ptw_resolves_riscv_pkg_from_sargantana_includes(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sargantana = os.path.join(repo, 'benchmarks', 'sargantana')
        roots = main.package_search_roots(self.ptw, ['sources'], 'packages')
        self.assertIn(sargantana, roots)
        self.assertLess(roots.index(os.path.dirname(self.ptw)), roots.index(sargantana))
        found = main.detect_packages_from_rtl(self.ptw, ['sources'], 'packages')
        by_name = {main.package_name_of_file(path): path for path in found}
        self.assertIn('mmu_pkg', by_name)
        self.assertIn('riscv_pkg', by_name)
        self.assertTrue(by_name['riscv_pkg'].startswith(sargantana))
        self.assertTrue(by_name['riscv_pkg'].endswith(
            os.path.join('includes', 'riscv_pkg.sv')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
