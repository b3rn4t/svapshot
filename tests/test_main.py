#!/usr/bin/env python3
"""Contract tests for the harness-construction stage in main.py.

main.py turns "here is an RTL file and some source directories" into everything
the formal tool needs: the DUT root the scaffolder is called with, the module type that
decides whether the property file gets a clocking block, the submodules and
packages the design depends on, the include paths, the fully expanded file list,
and the TCL that asks VC Formal for the reports the qualification stage parses.

Each of those is a place where a wrong answer is silent. A package resolved to
the wrong copy still compiles; a file list with an empty variable still gets
written; a module classified as sequential still produces a property file. The
failure appears several stages later wearing someone else's clothes, which is
why these are pinned here.

The modules under test come from Sargantana's execution stage.

    python3 tests/test_main.py
    python3 tests/test_main.py TestPackageResolution

Tests that need an ft_<module>/ tree drive the native scaffolder to build one,
and skip themselves if the Sargantana tree is unavailable.
"""

from __future__ import annotations

import contextlib
import functools
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS_DIR)
import _path_setup  # noqa: F401, E402

import coi  # noqa: E402
import main  # noqa: E402
import rtl_clocking  # noqa: E402

SARGANTANA = os.path.join(REPO, 'benchmarks', 'sargantana')
RTL_ROOT = os.path.join(SARGANTANA, 'rtl')
EXE_STAGE = os.path.join(RTL_ROOT, 'datapath/rtl/exe_stage/rtl')
INCLUDES = os.path.join(RTL_ROOT, 'datapath/includes')

#: Sequential, imports drac_pkg and riscv_pkg, instantiates div_4bits twice.
SEQUENTIAL = 'div_unit'
#: Combinational leaf, no clock anywhere in its interface.
CLOCKLESS = 'div_4bits'
#: Ten submodules and a `case` statement that used to be read as one of them.
WIDE = 'exe_stage'
#: Declares clk, clk_div, clk_div_valid and clk_out: four clock-looking ports.
MULTI_CLOCK = os.path.join(
    RTL_ROOT, 'datapath/rtl/exe_stage/rtl/fpu/src/common_cells/src/deprecated',
    'clock_divider_counter.sv')

_WORKSPACE = None


def _workspace() -> str:
    global _WORKSPACE
    if _WORKSPACE is None:
        _WORKSPACE = tempfile.mkdtemp(prefix='main_contract_')
    return _WORKSPACE


def tearDownModule():
    if _WORKSPACE and os.path.isdir(_WORKSPACE):
        shutil.rmtree(_WORKSPACE, ignore_errors=True)


requires_sargantana = unittest.skipUnless(
    os.path.isdir(EXE_STAGE), 'needs the sargantana tree')
requires_sargantana = unittest.skipUnless(
    os.path.isdir(EXE_STAGE),
    'needs the sargantana tree')


class TestInitialAssertionCap(unittest.TestCase):
    """The checker receives at most the experiment cap; the dump file does not."""

    def test_create_base_keeps_only_the_capped_prefix(self):
        blocks = []
        for index in range(3):
            blocks.append(
                f'a_p{index}: assert property (req |-> gnt)\n'
                'else begin\n'
                f'    $display("p{index}");\n'
                'end'
            )
        dump = '\n\n'.join(blocks) + '\n'
        with tempfile.TemporaryDirectory() as work:
            rtl = os.path.join(work, 'unit.sv')
            with open(rtl, 'w', encoding='utf-8') as handle:
                handle.write('module unit(input logic req, output logic gnt); endmodule\n')
            sva = os.path.join(work, 'ft_unit', 'sva')
            os.makedirs(sva, exist_ok=True)
            with open(os.path.join(sva, 'unit_prop.sv'), 'w', encoding='utf-8') as handle:
                handle.write(
                    'module unit_prop;\n'
                    '//====DESIGNER-ADDED-SVA====//\n'
                    'endmodule\n'
                )
            dump_path = os.path.join(work, 'initial_assertions.sv')
            with open(dump_path, 'w', encoding='utf-8') as handle:
                handle.write(dump)
            with mock.patch.dict(os.environ, {
                'SVAPSHOT_INITIAL_ASSERTIONS_PATH': dump_path,
                'SVAPSHOT_MAX_ASSERTIONS': '2',
            }, clear=False):
                previous = os.getcwd()
                os.chdir(work)
                try:
                    self.assertTrue(main.create_base_from_initial_assertions(rtl))
                finally:
                    os.chdir(previous)
            with open(dump_path, encoding='utf-8') as handle:
                self.assertEqual(handle.read(), dump)
            with open(os.path.join(sva, 'base'), encoding='utf-8') as handle:
                base = handle.read()
            self.assertIn('a_p0:', base)
            self.assertIn('a_p1:', base)
            self.assertNotIn('a_p2:', base)


class TestInitialAssertionGeneration(unittest.TestCase):
    """Initial generation is a direct RTL + SVA-rules model call."""

    def test_direct_generation_sends_rtl_and_rules_without_retrieval(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=(
                '---\n'
                'id: request_grant\n'
                'property: req_i |=> grant_o\n'
                'failure: Grant missing after request\n'
                '---\n'
            )))]
        )
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=mock.Mock(return_value=response))))
        with tempfile.TemporaryDirectory() as work:
            rtl = os.path.join(work, 'unit.sv')
            with open(rtl, 'w') as handle:
                handle.write('module unit(input logic req_i, output logic grant_o); endmodule')
            with mock.patch.object(main, 'OpenAI', return_value=fake_client), \
                    mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'},
                                    clear=False), \
                    mock.patch('os.getcwd', return_value=work):
                self.assertTrue(main.run_initial_assertion_generation(
                    rtl, 'gpt-4.1-mini'))

            with open(os.path.join(work, 'initial_assertions')) as handle:
                output = handle.read()
            self.assertIn('a_request_grant: assert property', output)
            request = fake_client.chat.completions.create.call_args.kwargs
            prompt = request['messages'][0]['content']
            self.assertIn('RTL MODULE:', prompt)
            self.assertIn('module unit', prompt)
            self.assertIn('Output ONLY assertion blocks', prompt)
            self.assertNotIn('rag2', prompt.lower())
            self.assertNotIn('embedding', prompt.lower())

    def test_missing_provider_key_fails_before_model_call(self):
        with tempfile.TemporaryDirectory() as work:
            rtl = os.path.join(work, 'unit.sv')
            with open(rtl, 'w') as handle:
                handle.write('module unit; endmodule')
            with mock.patch.object(main, 'OpenAI') as client, \
                    mock.patch.dict(os.environ, {}, clear=True):
                self.assertFalse(main.run_initial_assertion_generation(
                    rtl, 'gpt-4.1-mini'))
            client.assert_not_called()

    def test_reasoning_models_use_completion_token_parameter(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=(
                '---\nid: p\nproperty: req_i |=> grant_o\n---\n'
            )))])
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=mock.Mock(return_value=response))))
        with tempfile.TemporaryDirectory() as work:
            rtl = os.path.join(work, 'unit.sv')
            with open(rtl, 'w') as handle:
                handle.write('module unit(input logic req_i, output logic grant_o); endmodule')
            with mock.patch.object(main, 'OpenAI', return_value=fake_client), \
                    mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}, clear=False), \
                    mock.patch('os.getcwd', return_value=work):
                self.assertTrue(main.run_initial_assertion_generation(rtl, 'o4-mini'))

        request = fake_client.chat.completions.create.call_args.kwargs
        self.assertEqual(request['max_completion_tokens'], 32768)
        self.assertNotIn('max_tokens', request)
        self.assertNotIn('temperature', request)

    def _generate_with(self, llm_model, fake_client):
        """Run initial generation against a stubbed client, return the output."""
        with tempfile.TemporaryDirectory() as work:
            rtl = os.path.join(work, 'unit.sv')
            with open(rtl, 'w') as handle:
                handle.write(
                    'module unit(input logic req_i, output logic grant_o); endmodule')
            with mock.patch.object(main, 'OpenAI', return_value=fake_client), \
                    mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'},
                                    clear=False), \
                    mock.patch('os.getcwd', return_value=work):
                self.assertTrue(main.run_initial_assertion_generation(rtl, llm_model))
            with open(os.path.join(work, 'initial_assertions')) as handle:
                return handle.read()

    def test_responses_models_reach_the_responses_endpoint(self):
        """A v1/responses model must not be sent to v1/chat/completions.

        The chat endpoint answers 404 for those models, which killed the run at
        its first step. Which endpoint a model needs is read from the model
        table rather than guessed from its name.
        """
        fake_client = SimpleNamespace(
            responses=SimpleNamespace(create=mock.Mock(return_value=SimpleNamespace(
                output_text='---\nid: p\nproperty: req_i |=> grant_o\n---\n'))),
            chat=SimpleNamespace(completions=SimpleNamespace(create=mock.Mock())))

        output = self._generate_with('gpt-5.3-codex', fake_client)

        self.assertIn('a_p: assert property', output)
        fake_client.chat.completions.create.assert_not_called()
        request = fake_client.responses.create.call_args.kwargs
        self.assertIn('module unit', request['input'])
        self.assertIn('RTL MODULE:', request['input'])
        self.assertNotIn('messages', request)
        self.assertNotIn('temperature', request)

    def test_models_outside_the_table_still_use_the_chat_endpoint(self):
        """An unlisted name keeps the pre-table behaviour instead of failing."""
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(
                content='---\nid: p\nproperty: req_i |=> grant_o\n---\n'))])
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=mock.Mock(return_value=response))))

        output = self._generate_with('some-local-model', fake_client)

        self.assertIn('a_p: assert property', output)
        self.assertEqual(
            fake_client.chat.completions.create.call_args.kwargs['model'],
            'some-local-model')


class TestBindParameterExtraction(unittest.TestCase):
    def test_package_qualified_and_type_parameters_are_forwarded(self):
        rtl_text = '''
module fpnew_opgroup_block #(
  parameter fpnew_pkg::opgroup_e OpGroup = fpnew_pkg::ADDMUL,
  parameter int unsigned Width = 32,
  parameter logic EnableVectors = 1'b1,
  parameter fpnew_pkg::fmt_logic_t FpFmtMask = '1,
  parameter type TagType = logic,
  localparam int unsigned NUM_FORMATS = fpnew_pkg::NUM_FP_FORMATS
) (
  input logic clk_i
);
endmodule
'''
        with tempfile.TemporaryDirectory() as work:
            rtl = os.path.join(work, 'fpnew_opgroup_block.sv')
            bind = os.path.join(work, 'fpnew_opgroup_block_bind.svh')
            with open(rtl, 'w') as handle:
                handle.write(rtl_text)
            with open(bind, 'w') as handle:
                handle.write(
                    'bind fpnew_opgroup_block fpnew_opgroup_block_prop\n'
                    '\t#(\n\t\t.ASSERT_INPUTS (0)\n'
                    '\t) u_fpnew_opgroup_block_sva(.*);'
                )

            self.assertEqual(
                main.extract_parameters_from_rtl(rtl),
                ['OpGroup', 'Width', 'EnableVectors', 'FpFmtMask', 'TagType'],
            )
            self.assertEqual(main.ensure_bind_module_parameters(bind, rtl), 5)
            with open(bind) as handle:
                generated = handle.read()
            for name in ('OpGroup', 'Width', 'EnableVectors', 'FpFmtMask', 'TagType'):
                self.assertIn(f'.{name} ({name})', generated)
            self.assertNotIn('.NUM_FORMATS', generated)


@contextlib.contextmanager
def quiet():
    """main.py reports progress on stdout; tests only care about return values."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


@contextlib.contextmanager
def working_directory(path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield path
    finally:
        os.chdir(previous)


@contextlib.contextmanager
def environment(**overrides):
    """Set environment variables for the duration of the block; None unsets."""
    previous = {name: os.environ.get(name) for name in overrides}
    for name, value in overrides.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def rtl(module: str) -> str:
    return os.path.join(EXE_STAGE, module + '.sv')


def build_tree(module: str, module_type: str = 'sequential') -> str:
    """Scaffold ``ft_<module>/`` into a fresh directory and return that directory."""
    from scaffold import scaffold_harness

    root = tempfile.mkdtemp(dir=_workspace(), prefix=module + '_')
    saved = {key: os.environ.get(key) for key in ('DUT_ROOT', 'SVAPSHOT_ROOT')}
    try:
        os.environ['DUT_ROOT'] = SARGANTANA
        os.environ['SVAPSHOT_ROOT'] = root
        scaffold_harness(
            filename=os.path.relpath(rtl(module), SARGANTANA),
            sources=[RTL_ROOT],
            module_type=module_type,
            dut_root=SARGANTANA,
            output_root=root,
            work_dir=root,
        )
    except Exception as exc:
        raise AssertionError('scaffold failed for %s:\n%s' % (module, exc)) from exc
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return root


@functools.lru_cache(maxsize=None)
def packages_of(module: str) -> frozenset:
    with quiet():
        return frozenset(main.detect_packages_from_rtl(rtl(module), [RTL_ROOT], INCLUDES))


def read(path: str) -> str:
    with open(path, encoding='utf-8', errors='replace') as handle:
        return handle.read()


def write_module(name: str, text: str) -> str:
    """Put a small synthetic module in the workspace and return its path."""
    path = os.path.join(_workspace(), name + '.sv')
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(text)
    return path


class TestModulePathResolution(unittest.TestCase):
    """The DUT root handed to the scaffolder decides every path in the file list."""

    def test_a_module_inside_a_source_directory(self):
        with quiet():
            root, relative = main.compute_dut_root_and_relative_path(
                '/proj/rtl/core/alu.sv', ['/proj/rtl'])
        self.assertEqual(root, '/proj/rtl')
        self.assertEqual(relative, 'core/alu.sv')

    def test_the_first_matching_source_directory_wins(self):
        with quiet():
            root, relative = main.compute_dut_root_and_relative_path(
                '/proj/rtl/alu.sv', ['/other', '/proj/rtl'])
        self.assertEqual((root, relative), ('/proj/rtl', 'alu.sv'))

    def test_a_module_outside_every_source_falls_back_to_its_own_directory(self):
        with quiet():
            root, relative = main.compute_dut_root_and_relative_path(
                '/elsewhere/alu.sv', ['/proj/rtl'])
        self.assertEqual((root, relative), ('/elsewhere', 'alu.sv'))

    def test_a_sibling_with_a_shared_prefix_is_not_inside(self):
        # /proj/rtl_extra starts with /proj/rtl without being under it. Treating
        # it as contained yields a DUT root the module escapes with ../, which
        # the scaffolder then slices into a nonsense folder prefix.
        with quiet():
            root, relative = main.compute_dut_root_and_relative_path(
                '/proj/rtl_extra/core.sv', ['/proj/rtl'])
        self.assertEqual(root, '/proj/rtl_extra')
        self.assertEqual(relative, 'core.sv')
        self.assertNotIn('..', relative)

    def test_the_relative_path_never_escapes_the_root(self):
        for module_path, sources in (('/proj/rtl_extra/core.sv', ['/proj/rtl']),
                                     ('/proj/rtlx/a/b.sv', ['/proj/rtl']),
                                     ('/proj/rtl/a/b.sv', ['/proj/rtl'])):
            with self.subTest(module=module_path):
                with quiet():
                    root, relative = main.compute_dut_root_and_relative_path(
                        module_path, sources)
                self.assertEqual(os.path.normpath(os.path.join(root, relative)),
                                 module_path)

    def test_containment_needs_a_path_boundary(self):
        self.assertTrue(main._is_within('/a/b/c.sv', '/a/b'))
        self.assertTrue(main._is_within('/a/b', '/a/b'))
        self.assertTrue(main._is_within('/a/b/c.sv', '/a/b/'))
        self.assertFalse(main._is_within('/a/bc/d.sv', '/a/b'))
        self.assertFalse(main._is_within('/a', '/a/b'))


@requires_sargantana
class TestModuleTypeClassification(unittest.TestCase):
    """Sequential or combinational is read from the interface, not from a flag."""

    def test_a_module_with_a_clock_is_sequential(self):
        self.assertEqual(main.detect_module_type(rtl(SEQUENTIAL)), 'sequential')

    def test_a_module_without_a_clock_is_combinational(self):
        for module in (CLOCKLESS, 'branch_unit'):
            with self.subTest(module=module):
                self.assertEqual(main.detect_module_type(rtl(module)), 'combinational')

    def test_every_exe_stage_module_is_classified(self):
        modules = [f[:-3] for f in sorted(os.listdir(EXE_STAGE)) if f.endswith('.sv')]
        self.assertGreaterEqual(len(modules), 13)
        for module in modules:
            with self.subTest(module=module):
                self.assertIn(main.detect_module_type(rtl(module)),
                              ('sequential', 'combinational'))

    def test_clock_names_follow_the_usual_conventions(self):
        for name in ('clk', 'clk_i', 'clock', 'clock_i', 'core_clk', 'dst_clk_i', 'Clk_CI'):
            with self.subTest(name=name):
                self.assertTrue(main.looks_like_clock(name))
        for name in ('rst_i', 'enable', 'lock', 'block_i', 'instruction_i'):
            with self.subTest(name=name):
                self.assertFalse(main.looks_like_clock(name))

    def test_submodules_are_classified_by_the_same_rule(self):
        # A submodule judged by a different rule than its parent gets a
        # testbench built on a different notion of what a clock is.
        for module in (SEQUENTIAL, CLOCKLESS, 'store_buffer', 'branch_unit'):
            with self.subTest(module=module):
                with quiet():
                    self.assertEqual(main.detect_submodule_type(rtl(module)),
                                     main.detect_module_type(rtl(module)))


@requires_sargantana
class TestClockingReadFromTheBody(unittest.TestCase):
    """Which signal is the clock is answered by the RTL, not by its port names.

    A port list can hold several names that look like clocks and no rule over
    those names is reliable. The sensitivity list of a clocked block names the
    one the registers move on, and an asynchronous reset states its polarity
    there too.
    """

    def test_the_hard_case_is_read_correctly(self):
        # clock_divider_counter declares clk, clk_div, clk_div_valid and
        # clk_out. Only `always_ff @(posedge clk, negedge rstn)` distinguishes
        # them, and it also settles that rstn is asynchronous and active low.
        clocking = main.detect_clocking(MULTI_CLOCK)
        self.assertEqual(clocking.clock, 'clk')
        self.assertEqual(clocking.clock_edge, 'posedge')
        self.assertEqual(clocking.reset, 'rstn')
        self.assertTrue(clocking.reset_active_low)
        self.assertEqual(clocking.evidence, rtl_clocking.FROM_CLOCKED_BLOCK)

    def test_the_clock_is_never_a_signal_the_module_drives(self):
        # clk_out is the divided clock this module produces. Clocking the
        # property module on it would sample every property on the wrong edge
        # of the wrong signal.
        clocking = main.detect_clocking(MULTI_CLOCK)
        outputs = coi.build_signal_graph(read(MULTI_CLOCK)).ports_out
        self.assertIn('clk_out', outputs)
        self.assertNotIn(clocking.clock, outputs)

    def test_the_body_outranks_a_misleading_port_order(self):
        # Port order is the tie-breaker when there is no better evidence, and
        # here it points at the wrong signal.
        source = write_module('two_clocks', '''
module two_clocks (
  input  logic data_clk_i,
  input  logic sys_clk_i,
  input  logic rst_ni,
  input  logic d_i,
  output logic q_o
);
  always_ff @(posedge sys_clk_i or negedge rst_ni) begin
    if (!rst_ni) q_o <= 1'b0;
    else         q_o <= d_i;
  end
endmodule
''')
        clocking = main.detect_clocking(source)
        self.assertEqual(clocking.clock, 'sys_clk_i')
        self.assertEqual(clocking.reset, 'rst_ni')
        self.assertTrue(clocking.reset_active_low)

    def test_a_negedge_clock_is_reported_as_one(self):
        source = write_module('falling_edge', '''
module falling_edge (input logic clk_i, input logic d_i, output logic q_o);
  always_ff @(negedge clk_i) q_o <= d_i;
endmodule
''')
        clocking = main.detect_clocking(source)
        self.assertEqual((clocking.clock, clocking.clock_edge), ('clk_i', 'negedge'))

    def test_a_synchronous_reset_is_found_and_its_polarity_read_from_use(self):
        # It is not in the sensitivity list, so the port list supplies the name
        # and the way the body tests it supplies the polarity.
        source = write_module('sync_reset', '''
module sync_reset (input logic clk_i, input logic rst_i, input logic d_i,
                   output logic q_o);
  always_ff @(posedge clk_i) begin
    if (rst_i) q_o <= 1'b0;
    else       q_o <= d_i;
  end
endmodule
''')
        clocking = main.detect_clocking(source)
        self.assertEqual(clocking.reset, 'rst_i')
        self.assertFalse(clocking.reset_active_low)

    def test_an_active_low_synchronous_reset_is_told_apart(self):
        source = write_module('sync_reset_low', '''
module sync_reset_low (input logic clk_i, input logic reset_in, input logic d_i,
                       output logic q_o);
  always_ff @(posedge clk_i) begin
    if (!reset_in) q_o <= 1'b0;
    else           q_o <= d_i;
  end
endmodule
''')
        clocking = main.detect_clocking(source)
        # The name contains an "n" but is active high by name; only the usage
        # site says otherwise, and it is the one that counts.
        self.assertEqual(clocking.reset, 'reset_in')
        self.assertTrue(clocking.reset_active_low)

    def test_combinational_always_blocks_do_not_make_a_clock(self):
        source = write_module('comb_only', '''
module comb_only (input logic a_i, input logic b_i, output logic y_o);
  logic t;
  always_comb t = a_i & b_i;
  always @(a_i or b_i) y_o = t;
endmodule
''')
        clocking = main.detect_clocking(source)
        self.assertFalse(clocking.is_sequential)
        self.assertIsNone(clocking.clock)

    def test_a_clock_named_only_in_a_comment_does_not_count(self):
        source = write_module('commented_clock', '''
module commented_clock (input logic a_i, output logic y_o);
  // always_ff @(posedge clk_i) y_o <= a_i;
  /* always_ff @(posedge other_clk) y_o <= a_i; */
  assign y_o = a_i;
endmodule
''')
        self.assertFalse(main.detect_clocking(source).is_sequential)

    def test_a_structural_module_is_sequential_through_its_clock_port(self):
        # Forty-one modules under the execution stage instantiate clocked
        # children without a clocked block of their own. Body evidence extends
        # the port heuristic; it does not replace it.
        source = write_module('structural', '''
module structural (input logic clk_i, input logic rstn_i, input logic d_i,
                   output logic q_o);
  child u_child (.clk_i(clk_i), .rstn_i(rstn_i), .d_i(d_i), .q_o(q_o));
endmodule
''')
        clocking = main.detect_clocking(source)
        self.assertTrue(clocking.is_sequential)
        self.assertEqual(clocking.clock, 'clk_i')
        self.assertEqual(clocking.evidence, rtl_clocking.FROM_CLOCK_PORT)

    def test_a_module_whose_header_cannot_be_parsed_is_still_classified(self):
        # vmul.sv carries an attribute before the module keyword, which defeats
        # the header parse; its always_ff blocks are unambiguous all the same.
        vmul = os.path.join(RTL_ROOT, 'datapath/rtl/exe_stage/rtl/simd/vmul.sv')
        if not os.path.isfile(vmul):
            self.skipTest('vmul.sv not present')
        clocking = main.detect_clocking(vmul)
        self.assertTrue(clocking.is_sequential)
        self.assertEqual(clocking.clock, 'clk_i')
        self.assertEqual(clocking.evidence, rtl_clocking.FROM_CLOCKED_BLOCK)

    def test_a_file_with_no_module_is_not_classified(self):
        empty = write_module('no_module', '// just a comment\n')
        self.assertIsNone(main.detect_clocking(empty))
        self.assertIsNone(main.detect_module_type(empty))
        self.assertIsNone(main.detect_clocking('/no/such/file.sv'))

    def test_the_chosen_clock_is_a_signal_of_the_module(self):
        # The clocking block cannot reference a name the module never declares.
        for module in (SEQUENTIAL, WIDE, 'store_buffer'):
            with self.subTest(module=module):
                clocking = main.detect_clocking(rtl(module))
                self.assertIn(clocking.clock,
                              coi.build_signal_graph(read(rtl(module))).all_signals)


@requires_sargantana
class TestModuleTypeIsPerModule(unittest.TestCase):
    """A submodule is classified on its own RTL, whatever its parent is.

    The flow used to take the top module's type from the command line and only
    detect for submodules, so a combinational child under a sequential parent
    inherited a clocking block it could not elaborate with, and a caller had no
    way to express the difference.
    """

    def test_a_combinational_child_under_a_sequential_parent(self):
        # div_unit is clocked and instantiates div_4bits twice; div_4bits is
        # pure combinational logic. This is the pair that broke.
        with quiet():
            children = main.detect_submodules(rtl(SEQUENTIAL))
        self.assertEqual(children, {CLOCKLESS})
        self.assertEqual(main.detect_module_type(rtl(SEQUENTIAL)), 'sequential')
        self.assertEqual(main.detect_submodule_type(rtl(CLOCKLESS)), 'combinational')

    def test_the_execution_stage_children_are_not_all_the_same(self):
        # exe_stage is sequential; its children are a mix, and each is asked
        # separately.
        self.assertEqual(main.detect_module_type(rtl(WIDE)), 'sequential')
        with quiet():
            children = sorted(main.detect_submodules(rtl(WIDE)))
        types = {}
        for child in children:
            path = rtl(child)
            if os.path.isfile(path):
                types[child] = main.detect_submodule_type(path)
        self.assertGreaterEqual(len(types), 4)
        self.assertEqual(set(types.values()), {'sequential', 'combinational'},
                         'expected a mix, got %r' % types)

    def test_a_request_that_contradicts_the_rtl_is_overridden(self):
        for module, requested, expected in (
                (CLOCKLESS, 'sequential', 'combinational'),
                (SEQUENTIAL, 'combinational', 'sequential')):
            with self.subTest(module=module, requested=requested):
                with quiet() as output:
                    resolved = main.resolve_module_type(rtl(module), requested)
                self.assertEqual(resolved, expected)
                self.assertIn(requested, output.getvalue())

    def test_the_reason_for_overriding_names_the_evidence(self):
        with quiet() as output:
            main.resolve_module_type(rtl(SEQUENTIAL), 'combinational')
        self.assertIn('clk_i', output.getvalue())

    def test_auto_says_nothing_and_returns_the_detected_type(self):
        for module, expected in ((SEQUENTIAL, 'sequential'), (CLOCKLESS, 'combinational')):
            with self.subTest(module=module):
                with quiet() as output:
                    self.assertEqual(main.resolve_module_type(rtl(module), 'auto'), expected)
                self.assertEqual(output.getvalue(), '')

    def test_agreeing_with_the_rtl_says_nothing_either(self):
        with quiet() as output:
            main.resolve_module_type(rtl(SEQUENTIAL), 'sequential')
        self.assertEqual(output.getvalue(), '')

    def test_a_file_with_no_module_falls_back_to_the_safe_label(self):
        # A clocking block on a module that turns out to have no clock cannot
        # elaborate; the other way round only costs a weaker property set.
        empty = write_module('nothing_here', '// no module\n')
        with quiet():
            self.assertEqual(main.resolve_module_type(empty, 'auto'), 'combinational')
            self.assertEqual(main.resolve_module_type(empty, 'sequential'), 'sequential')


class TestModuleTypeIsNotAskedFor(unittest.TestCase):
    """The command line no longer has to state something the RTL knows."""

    @staticmethod
    def _parse(argv):
        return main.parse_arguments(argv)

    def test_the_type_can_be_omitted(self):
        args = self._parse(['alu.sv', 'a/model', 'golden', 'medium'])
        self.assertEqual(args.module_type, 'auto')
        self.assertEqual(args.execution_type, 'golden')
        self.assertEqual(args.verbosity, 'medium')

    def test_an_existing_command_line_still_works(self):
        args = self._parse(['alu.sv', 'a/model', 'sequential', 'golden', 'low'])
        self.assertEqual(args.module_type, 'sequential')
        self.assertEqual(args.execution_type, 'golden')
        self.assertEqual(args.verbosity, 'low')

    def test_verbosity_defaults_to_low_when_omitted(self):
        args = self._parse(['alu.sv', 'a/model', 'golden'])
        self.assertEqual(args.module_type, 'auto')
        self.assertEqual(args.execution_type, 'golden')
        self.assertEqual(args.verbosity, 'low')

    def test_parent_rtl_flag_is_accepted(self):
        args = self._parse([
            'tlb.sv', 'a/model', 'golden', 'medium',
            '--parent-rtl', 'mmu_top.sv',
        ])
        self.assertEqual(args.parent_rtl, 'mmu_top.sv')


@requires_sargantana
class TestSubmoduleDetection(unittest.TestCase):
    """The submodule list drives a testbench and a property set per entry."""

    def test_a_leaf_module_has_no_submodules(self):
        with quiet():
            self.assertEqual(main.detect_submodules(rtl('store_buffer')), set())

    def test_repeated_instances_of_one_module_count_once(self):
        # div_unit instantiates div_4bits twice.
        with quiet():
            self.assertEqual(main.detect_submodules(rtl(SEQUENTIAL)), {'div_4bits'})

    def test_the_execution_stage_units_are_all_found(self):
        with quiet():
            found = main.detect_submodules(rtl(WIDE))
        self.assertEqual(found, {
            'alu', 'branch_unit', 'div_unit', 'fpu_drac_wrapper', 'mem_unit',
            'mul_unit', 'score_board_scalar', 'score_board_simd', 'simd_unit',
            'vagu'})

    def test_a_block_ender_is_not_a_submodule(self):
        # exe_stage used to report `endcase` because the pattern allowed the
        # module name and the instance name to sit on different lines, pairing
        # the keyword that closed one block with the instantiation after it.
        with quiet():
            found = main.detect_submodules(rtl(WIDE))
        self.assertNotIn('endcase', found)
        self.assertFalse([name for name in found if name.startswith('end')])

    def test_a_keyword_followed_by_an_instantiation_is_rejected(self):
        source = os.path.join(_workspace(), 'block_ender.sv')
        with open(source, 'w', encoding='utf-8') as handle:
            handle.write(
                'module top;\n'
                '  always_comb begin\n'
                '    case (sel)\n'
                '      default: x = y;\n'
                '    endcase\n'
                '  end\n'
                '  real_child u_child (\n'
                '    .a(a)\n'
                '  );\n'
                'endmodule\n')
        with quiet():
            found = main.detect_submodules(source)
        self.assertEqual(found, {'real_child'})

    def test_a_parameterised_instantiation_spanning_lines_is_found(self):
        # clock_divider puts the module name on one line and #( on the next.
        source = os.path.join(_workspace(), 'spanning.sv')
        with open(source, 'w', encoding='utf-8') as handle:
            handle.write(
                'module top;\n'
                '  child_module\n'
                '  #(\n'
                '    .WIDTH(8)\n'
                '  )\n'
                '  u_child (\n'
                '    .a(a)\n'
                '  );\n'
                'endmodule\n')
        with quiet():
            self.assertEqual(main.detect_submodules(source), {'child_module'})

    def test_a_missing_file_is_reported_not_raised(self):
        with quiet():
            self.assertEqual(main.detect_submodules('/no/such/module.sv'), set())


@requires_sargantana
class TestPackageResolution(unittest.TestCase):
    """A package must resolve to the file the design actually defines."""

    def test_the_packages_a_module_imports_are_found(self):
        names = {main.package_name_of_file(p) for p in packages_of(SEQUENTIAL)}
        self.assertEqual(names, {'drac_pkg', 'riscv_pkg'})

    def test_every_resolved_file_declares_the_package_it_stands_for(self):
        # Matching on filename alone accepts include headers and unrelated
        # files that merely share a name.
        for module in (SEQUENTIAL, 'store_buffer', WIDE):
            for path in packages_of(module):
                with self.subTest(module=module, path=os.path.basename(path)):
                    self.assertIsNotNone(main.package_name_of_file(path))

    def test_packages_resolve_inside_the_design_tree(self):
        # drac_pkg lives in sargantana/includes, a sibling of the sargantana/rtl
        # source root, so it is only reachable by widening the search to the
        # repository that owns the DUT.
        by_name = {main.package_name_of_file(p): p for p in packages_of(SEQUENTIAL)}
        for name in ('drac_pkg', 'riscv_pkg'):
            with self.subTest(package=name):
                self.assertTrue(by_name[name].startswith(SARGANTANA),
                                '%s resolved outside the design: %s' % (name, by_name[name]))

    def test_a_package_needed_only_by_another_package_is_pulled_in(self):
        # div_4bits imports drac_pkg and nothing else. drac_pkg is written
        # against riscv_pkg, so a file list without riscv_pkg fails to analyse
        # on a package the design never mentions by name.
        with quiet():
            direct = main.detect_packages_from_rtl(rtl(CLOCKLESS), [RTL_ROOT], INCLUDES)
            by_name = {main.package_name_of_file(p): p for p in direct}
            closed = main.close_package_dependencies(
                dict(by_name), [RTL_ROOT], INCLUDES, rtl(CLOCKLESS))

        self.assertIn('drac_pkg', by_name)
        self.assertNotIn('riscv_pkg', by_name)
        self.assertIn('riscv_pkg', closed)
        self.assertTrue(closed['riscv_pkg'].startswith(SARGANTANA))

    def test_a_package_that_pulls_in_another_is_listed_after_it(self):
        with quiet():
            closed = main.close_package_dependencies(
                {'drac_pkg': os.path.join(SARGANTANA, 'includes', 'drac_pkg.sv')},
                [RTL_ROOT], INCLUDES, rtl(CLOCKLESS))
            ordered = main.order_packages_by_dependency(closed)
        names = [main.package_name_of_file(p) for p in ordered]
        self.assertLess(names.index('riscv_pkg'), names.index('drac_pkg'))

    def test_package_texts_accept_a_dependency_ordered_list(self):
        # discover_used_packages returns paths, not a name→path mapping.
        # Calling .items() on that list crashed PHASE 1 for fpnew_opgroup_block.
        root = tempfile.mkdtemp(dir=_workspace(), prefix='pkgtexts_')
        path = os.path.join(root, 'fpnew_pkg.sv')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('package fpnew_pkg;\n  localparam int W = 64;\nendpackage\n')
        with mock.patch.object(main, 'discover_used_packages', return_value=[path]):
            texts = main._package_texts_for_parent('unused.sv', [root], root)
        self.assertEqual(set(texts), {'fpnew_pkg'})
        self.assertIn('localparam int W = 64', texts['fpnew_pkg'])

    def test_the_closure_reaches_through_several_packages(self):
        root = tempfile.mkdtemp(dir=_workspace(), prefix='chain_')
        for name, body in (('leaf_pkg', 'localparam int W = 8;'),
                           ('middle_pkg', 'localparam int V = leaf_pkg::W;'),
                           ('top_pkg', 'localparam int U = middle_pkg::V;')):
            with open(os.path.join(root, name + '.sv'), 'w', encoding='utf-8') as handle:
                handle.write('package %s;\n  %s\nendpackage\n' % (name, body))

        with quiet():
            closed = main.close_package_dependencies(
                {'top_pkg': os.path.join(root, 'top_pkg.sv')}, [root], root)
        self.assertEqual(set(closed), {'top_pkg', 'middle_pkg', 'leaf_pkg'})

    def test_the_closure_terminates_on_a_cycle(self):
        root = tempfile.mkdtemp(dir=_workspace(), prefix='cycle_')
        for name, other in (('ping_pkg', 'pong_pkg'), ('pong_pkg', 'ping_pkg')):
            with open(os.path.join(root, name + '.sv'), 'w', encoding='utf-8') as handle:
                handle.write('package %s;\n  localparam int X = %s::Y;\nendpackage\n'
                             % (name, other))

        with quiet():
            closed = main.close_package_dependencies(
                {'ping_pkg': os.path.join(root, 'ping_pkg.sv')}, [root], root)
        self.assertEqual(set(closed), {'ping_pkg', 'pong_pkg'})

    def test_a_same_named_copy_on_the_fallback_path_does_not_win(self):
        # packages/ is one of the directories searched when nothing better is
        # found. A stale copy there used to shadow the design's own definition,
        # so the tool compiled a different drac_pkg than the RTL was written
        # against and the mismatch surfaced as unrelated elaboration errors.
        sandbox = tempfile.mkdtemp(dir=_workspace(), prefix='decoy_')
        os.makedirs(os.path.join(sandbox, 'packages'))
        decoy = os.path.join(sandbox, 'packages', 'drac_pkg.sv')
        with open(decoy, 'w', encoding='utf-8') as handle:
            handle.write('package drac_pkg;\n  // an older, different copy\n endpackage\n')

        with working_directory(sandbox), quiet():
            resolved = main.find_package_files(
                {'drac_pkg'}, [RTL_ROOT], INCLUDES, rtl(SEQUENTIAL))

        self.assertEqual(len(resolved), 1)
        chosen = resolved.pop()
        self.assertNotEqual(os.path.abspath(chosen), os.path.abspath(decoy))
        self.assertTrue(chosen.startswith(SARGANTANA), chosen)

    def test_the_design_tree_of_a_module_is_its_repository(self):
        self.assertEqual(main.design_tree_root(rtl(SEQUENTIAL)), SARGANTANA)

    def test_nested_mmu_clone_still_sees_sargantana_packages(self):
        ptw = os.path.join(SARGANTANA, 'rtl', 'mmu', 'rtl', 'ptw', 'ptw.sv')
        if not os.path.isfile(ptw):
            self.skipTest('ptw.sv is not in this checkout')
        trees = main.suite_package_trees(ptw)
        self.assertIn(SARGANTANA, trees)
        self.assertTrue(any(os.path.basename(tree) == 'mmu' for tree in trees))

    def test_search_roots_put_the_design_before_the_fallbacks(self):
        roots = main.package_search_roots(rtl(SEQUENTIAL), [RTL_ROOT], INCLUDES)
        self.assertEqual(roots[0], EXE_STAGE)
        self.assertIn(SARGANTANA, roots)
        self.assertLess(roots.index(SARGANTANA), len(roots))

    def test_a_package_name_comes_from_the_declaration(self):
        source = os.path.join(_workspace(), 'oddly_named.sv')
        with open(source, 'w', encoding='utf-8') as handle:
            handle.write('// comment\npackage actual_pkg;\nendpackage\n')
        self.assertEqual(main.package_name_of_file(source), 'actual_pkg')
        self.assertIsNone(main.package_name_of_file(rtl(SEQUENTIAL)))

    def test_dependencies_are_ordered_before_their_dependents(self):
        base = tempfile.mkdtemp(dir=_workspace(), prefix='order_')
        low = os.path.join(base, 'low_pkg.sv')
        high = os.path.join(base, 'high_pkg.sv')
        with open(low, 'w', encoding='utf-8') as handle:
            handle.write('package low_pkg;\n  parameter W = 8;\nendpackage\n')
        with open(high, 'w', encoding='utf-8') as handle:
            handle.write('package high_pkg;\n  parameter X = low_pkg::W;\nendpackage\n')
        ordered = main.order_packages_by_dependency({'high_pkg': high, 'low_pkg': low})
        self.assertLess(ordered.index(low), ordered.index(high))

    def test_the_index_skips_verification_trees(self):
        # A package defined under tb/ or vendor/ is not part of the DUT and
        # must not be compiled into the analysis.
        index = main.index_package_definitions([RTL_ROOT])
        for name, path in index.items():
            with self.subTest(package=name):
                self.assertNotRegex(path, r'/(tb|test|tests|vendor|doc|docs)/')


@requires_sargantana
class TestIncludeDiscovery(unittest.TestCase):
    """`include resolution needs the directory and its parent on the path."""

    def test_a_header_directory_and_its_parent_are_both_offered(self):
        # Headers are referenced both as `registers.svh and as
        # `common_cells/registers.svh, which need different +incdir entries.
        dirs = main.discover_include_dirs([EXE_STAGE])
        header_dirs = [d for d in dirs if d.endswith('include/common_cells')]
        self.assertTrue(header_dirs)
        for directory in header_dirs:
            with self.subTest(directory=directory):
                self.assertIn(os.path.dirname(directory), dirs)

    def test_the_result_is_sorted_and_free_of_duplicates(self):
        dirs = main.discover_include_dirs([EXE_STAGE, EXE_STAGE])
        self.assertEqual(dirs, sorted(set(dirs)))

    def test_missing_roots_are_ignored(self):
        self.assertEqual(main.discover_include_dirs(['/no/such/dir', None, '']), [])


@requires_sargantana
class TestPackageInjection(unittest.TestCase):
    """The other half of the handoff: the scaffolder mirrors types, main.py imports them."""

    @classmethod
    def setUpClass(cls):
        cls.root = build_tree(SEQUENTIAL)
        cls.prop_path = os.path.join(
            cls.root, 'ft_' + SEQUENTIAL, 'sva', SEQUENTIAL + '_prop.sv')
        with working_directory(cls.root), quiet():
            cls.ok = main.enhanced_create_manual_sub_and_inject_packages(
                rtl(SEQUENTIAL), [RTL_ROOT], INCLUDES)
        cls.prop_text = read(cls.prop_path)

    def test_the_step_succeeds(self):
        self.assertTrue(self.ok)

    def test_the_property_module_imports_what_the_dut_imports(self):
        dut_imports = set(re.findall(r'^\s*import\s+(\w+)::',
                                     read(rtl(SEQUENTIAL)), re.MULTILINE))
        injected = set(re.findall(r'^\s*import\s+(\w+)::\*;',
                                  self.prop_text, re.MULTILINE))
        self.assertEqual(dut_imports, {'drac_pkg', 'riscv_pkg'})
        self.assertTrue(dut_imports <= injected,
                        'missing %s' % sorted(dut_imports - injected))

    def test_imported_names_are_declared_by_a_real_package(self):
        # Deriving the name from the filename produces an import of something
        # that does not exist as soon as the two differ.
        declared = {main.package_name_of_file(p)
                    for p in main.index_package_definitions([SARGANTANA]).values()}
        for name in re.findall(r'^\s*import\s+(\w+)::\*;', self.prop_text, re.MULTILINE):
            with self.subTest(package=name):
                self.assertIn(name, declared)

    def test_the_imports_sit_where_systemverilog_allows_them(self):
        # A package import belongs between the module name and the parameter
        # list; anywhere else and the header stops parsing.
        name, params, ports, _ = coi.split_module_header(
            coi.strip_sv_noise(self.prop_text))
        self.assertEqual(name, SEQUENTIAL + '_prop')
        self.assertIn('ASSERT_INPUTS', params)
        self.assertIn('instruction_i', ports)
        header = self.prop_text.split('#(', 1)[0]
        self.assertIn('import drac_pkg::*;', header)

    def test_the_mirrored_interface_is_untouched(self):
        graph = coi.build_signal_graph(self.prop_text, SEQUENTIAL + '_prop')
        rtl_graph = coi.build_signal_graph(read(rtl(SEQUENTIAL)), SEQUENTIAL)
        self.assertEqual(graph.ports_in, rtl_graph.ports_in | rtl_graph.ports_out)

    def test_running_the_step_again_does_not_duplicate_imports(self):
        with working_directory(self.root), quiet():
            main.enhanced_create_manual_sub_and_inject_packages(
                rtl(SEQUENTIAL), [RTL_ROOT], INCLUDES)
        imports = re.findall(r'^\s*import\s+(\w+)::\*;', read(self.prop_path), re.MULTILINE)
        self.assertEqual(len(imports), len(set(imports)))

    def test_the_manual_file_list_names_the_design_packages(self):
        manual_sub = read(os.path.join(self.root, 'ft_' + SEQUENTIAL, 'manual_sub.vc'))
        for package in ('drac_pkg.sv', 'riscv_pkg.sv'):
            with self.subTest(package=package):
                self.assertIn(os.path.join(SARGANTANA, 'includes', package), manual_sub)


@requires_sargantana
class TestFileListExpansion(unittest.TestCase):
    """files_vcf.vc is handed to VCS, which does no Tcl expansion of its own."""

    @classmethod
    def setUpClass(cls):
        cls.root = build_tree(SEQUENTIAL)
        cls.ft_dir = os.path.join(cls.root, 'ft_' + SEQUENTIAL)
        with working_directory(cls.root), \
                environment(DUT_ROOT=SARGANTANA, SVAPSHOT_ROOT=cls.root), quiet():
            cls.ok = main.generate_fpv_vcf_tcl(rtl(SEQUENTIAL), 'sequential')
        cls.vc_text = read(os.path.join(cls.ft_dir, 'files_vcf.vc'))

    def test_the_step_succeeds(self):
        self.assertTrue(self.ok)

    def test_no_tcl_variable_survives_expansion(self):
        self.assertNotIn('${', self.vc_text)

    def test_every_listed_source_file_exists(self):
        entries = [line.strip() for line in self.vc_text.splitlines()
                   if line.strip() and not line.startswith(('+', '-y', '//'))]
        files = [e for e in entries if not e.startswith('-f ')]
        self.assertTrue(files)
        for path in files:
            with self.subTest(path=path):
                self.assertTrue(os.path.isfile(path), path)

    def test_the_dut_the_property_module_and_the_bind_are_all_listed(self):
        self.assertIn(rtl(SEQUENTIAL), self.vc_text)
        self.assertIn(SEQUENTIAL + '_prop.sv', self.vc_text)
        self.assertIn(SEQUENTIAL + '_bind.svh', self.vc_text)

    def test_include_directories_are_prepended(self):
        self.assertTrue(self.vc_text.startswith('+incdir+'))


@requires_sargantana
class TestFileListRefusesBrokenPaths(unittest.TestCase):
    """An unset path variable used to produce a file list pointing at nothing."""

    @classmethod
    def setUpClass(cls):
        cls.root = build_tree(SEQUENTIAL)
        cls.ft_dir = os.path.join(cls.root, 'ft_' + SEQUENTIAL)
        with working_directory(cls.root), \
                environment(DUT_ROOT=None, SVAPSHOT_ROOT=cls.root):
            with quiet() as output:
                cls.ok = main.generate_fpv_vcf_tcl(rtl(SEQUENTIAL), 'sequential')
            cls.output = output.getvalue()

    def test_it_refuses(self):
        # ${DUT_ROOT}/rtl/... used to expand to /rtl/..., an absolute path at
        # the filesystem root that does not exist, written without complaint.
        self.assertFalse(self.ok)

    def test_it_names_the_variable_that_is_missing(self):
        self.assertIn('DUT_ROOT', self.output)

    def test_no_file_list_is_written(self):
        self.assertFalse(os.path.isfile(os.path.join(self.ft_dir, 'files_vcf.vc')))


@requires_sargantana
class TestGeneratedVcFormalScript(unittest.TestCase):
    """The TCL has to ask for every report the qualification stage parses."""

    @classmethod
    def setUpClass(cls):
        cls.sequential = cls._generate(SEQUENTIAL, 'sequential')
        cls.combinational = cls._generate(CLOCKLESS, 'combinational')

    @staticmethod
    def _generate(module, module_type):
        root = build_tree(module, module_type)
        with working_directory(root), \
                environment(DUT_ROOT=SARGANTANA, SVAPSHOT_ROOT=root), quiet():
            assert main.generate_fpv_vcf_tcl(rtl(module), module_type)
        return read(os.path.join(root, 'ft_' + module, 'FPV_vcf.tcl'))

    def test_the_top_module_is_the_dut(self):
        self.assertIn('set top %s' % SEQUENTIAL, self.sequential)
        self.assertIn('elaborate -cov all -sva $top', self.sequential)

    def test_top_level_coverage_is_the_default(self):
        self.assertIn('set collect_coverage 1', self.sequential)
        self.assertIn('set hierarchical_coverage 0', self.sequential)
        self.assertIn('elaborate -cov all -sva $top', self.sequential)
        self.assertIn(
            'compute_formal_core_coverage -par_task FPV -structural -block',
            self.sequential,
        )

    def test_the_fast_path_has_no_coverage_instrumentation(self):
        fast_guard = self.sequential.index('if {!$collect_coverage} {')
        plain_elaboration = self.sequential.index(
            'elaborate -sva $top', fast_guard)
        coverage_elaboration = self.sequential.index(
            'elaborate -cov all -sva $top', fast_guard)
        self.assertLess(plain_elaboration, coverage_elaboration)
        self.assertIn(
            'COVERAGE_ANALYSIS_SKIPPED: SVAPSHOT_COVERAGE=0',
            self.sequential,
        )

    def test_post_proof_coverage_is_guarded_as_one_unit(self):
        proof_report = self.sequential.index('report_fv -verbose')
        coverage_guard = self.sequential.index(
            'if {$collect_coverage} {', proof_report)
        for command in (
                'compute_formal_core -block',
                'compute_reduced_constraints -block',
                'report_assertion_density',
                'report_fv_complexity',
                'compute_formal_core_coverage'):
            with self.subTest(command=command):
                self.assertGreater(self.sequential.index(command), coverage_guard)

    def test_hierarchical_coverage_is_explicitly_opt_in(self):
        # -cm_libs vy instruments modules discovered through -y/-v. On
        # fpnew_top this grew the model to ~1.1M operators / 267k flop bits and
        # VC Static disconnected at check_fv, while ordinary FPV completed.
        self.assertIn('env(SVAPSHOT_HIERARCHICAL_COVERAGE)', self.sequential)
        guard = self.sequential.index('if {$hierarchical_coverage} {')
        hierarchy_elaboration = (
            'elaborate -cov all -sva $top -vcs "-cm_libs vy"')
        self.assertGreater(self.sequential.index(hierarchy_elaboration), guard)
        self.assertIn('set hierarchical_coverage 0', self.sequential[:guard])

    def test_the_clock_and_reset_are_carried_over_from_the_scaffold(self):
        self.assertIn('create_clock clk_i -period 100', self.sequential)
        self.assertIn('create_reset rstn_i -sense low', self.sequential)

    def test_a_combinational_module_samples_on_formal_clk(self):
        # Commands only: a comment mentioning either command is not a clock
        # constraint, and reading the whole file as one string made explaining
        # anything in the script a way to fail this test.
        commands = [line for line in self.combinational.splitlines()
                    if line.strip() and not line.lstrip().startswith('#')]
        clock_lines = [line for line in commands if 'create_clock' in line]
        self.assertEqual(len(clock_lines), 1)
        self.assertIn('formal_clk', clock_lines[0])
        self.assertEqual([line for line in commands if 'create_reset' in line], [])

    def test_display_system_tasks_are_waived_at_analyze(self):
        # Checkers keep $display for simulation; Simon would otherwise emit
        # Warning-[SM_UST] once per callsite during analyze.
        self.assertIn('suppress_message SM_UST', self.sequential)
        waiver = self.sequential.index('suppress_message SM_UST')
        analyze = self.sequential.index(
            'analyze -format sverilog -vcs "-f $files_vcf"')
        self.assertLess(waiver, analyze)

    def test_vacuity_and_witness_goals_are_enabled(self):
        # Without these a proof whose antecedent is never reachable is reported
        # exactly like a real one.
        self.assertIn('set_fml_var fml_vacuity_on true', self.sequential)
        self.assertIn('set_fml_var fml_witness_on true', self.sequential)

    def test_the_proof_blocks_so_results_are_complete(self):
        self.assertIn('check_fv -block', self.sequential)

    def test_a_failed_proof_command_does_not_abort_the_script(self):
        # VC Formal stops at the first uncaught error. An empty or uncompilable
        # property set made check_fv fail, and the reports the agent parses were
        # never written, so the run came back with nothing to act on.
        self.assertIn('if {[catch {check_fv -block} check_fv_error]} {', self.sequential)
        self.assertIn('CHECK_FV_FAILED', self.sequential)

    def test_every_report_the_pipeline_parses_is_requested(self):
        for command, consumer in (
                ('report_fv -verbose', 'per-property status'),
                ('compute_formal_core', 'assumption dependence'),
                ('report_formal_core -list', 'formal core'),
                ('compute_reduced_constraints', 'needed constraints'),
                ('report_constraints -verbose', 'constraint sanity'),
                ('report_assertion_density', 'checker coverage'),
                ('report_fv_complexity', 'per-property COI')):
            with self.subTest(report=consumer):
                self.assertIn(command, self.sequential)

    def test_the_density_report_is_split_by_scope(self):
        # proof_status.py keys on these markers to keep registers and primary
        # inputs apart instead of averaging them into one number.
        self.assertIn('DENSITY_SCOPE reg', self.sequential)
        self.assertIn('DENSITY_SCOPE pi', self.sequential)
        self.assertIn('-type reg', self.sequential)
        self.assertIn('-type pi', self.sequential)

    def test_the_coi_report_is_delimited_per_property(self):
        self.assertIn('PROPERTY_COI_BEGIN', self.sequential)
        self.assertIn('PROPERTY_COI_END', self.sequential)

    def test_counterexample_traces_are_dumped_after_the_proof(self):
        # Semantic repair needs a witness on disk. fvtrace is official FSDB
        # export; it is not coverage analysis and must run even when
        # SVAPSHOT_COVERAGE=0.
        proof_report = self.sequential.index('report_fv -verbose')
        dump = self.sequential.index('CEX_DUMP_BEGIN')
        coverage_guard = self.sequential.index(
            'if {$collect_coverage} {', proof_report)
        self.assertLess(proof_report, dump)
        self.assertLess(dump, coverage_guard)
        self.assertIn('fvtrace -property', self.sequential)
        self.assertIn('$report_dir/cex/', self.sequential)
        self.assertIn('CEX_DUMP_END', self.sequential)

    def test_the_run_instruction_matches_the_runner(self):
        # The header line is what a reader copies. It used to suggest -o, which
        # vcf does not accept, so the copied command printed a usage dump.
        self.assertIn(
            './fpv_app_scripts/run_vcf_batch.sh %s' % SEQUENTIAL,
            self.sequential,
        )
        self.assertNotRegex(self.sequential, r'vcf -f .* -o ')

    def test_reports_are_written_where_the_parsers_look(self):
        self.assertIn('set report_dir "vcf_projs/%s/reports"' % SEQUENTIAL,
                      self.sequential)

    def test_reports_and_coverage_cannot_survive_from_an_older_run(self):
        self.assertIn('file delete -force $report_dir', self.sequential)
        self.assertIn('file delete -force $covdb.vdb $covdb.el', self.sequential)
        self.assertLess(
            self.sequential.index('file delete -force $report_dir'),
            self.sequential.index('file mkdir $report_dir'),
        )
        self.assertLess(
            self.sequential.index('file delete -force $covdb.vdb $covdb.el'),
            self.sequential.index('check_fv -block'),
        )

    def test_optional_reports_cannot_abort_the_run(self):
        # A command missing from a given VC Formal version must degrade the
        # report, not cost the agent its proof results.
        for command in ('compute_formal_core', 'report_assertion_density',
                        'compute_reduced_constraints'):
            with self.subTest(command=command):
                line = next(l for l in self.sequential.splitlines() if command in l)
                self.assertTrue(line.lstrip().startswith('catch {')
                                or 'catch {' in self.sequential.split(line)[0].splitlines()[-1],
                                'not wrapped in catch: %s' % line)


@requires_sargantana
class TestParentElaboratePvalues(unittest.TestCase):
    """Resolved parent constants must reach VCS via ``-pvalue``."""

    def test_pvalue_flags_are_passed_to_every_elaborate(self):
        root = build_tree(SEQUENTIAL)
        with working_directory(root), \
                environment(DUT_ROOT=SARGANTANA, SVAPSHOT_ROOT=root), quiet():
            assert main.generate_fpv_vcf_tcl(
                rtl(SEQUENTIAL), 'sequential',
                elaboration_overrides={
                    'Width': '64',
                    'EnableVectors': "1'b1",
                },
            )
        tcl = read(os.path.join(root, 'ft_' + SEQUENTIAL, 'FPV_vcf.tcl'))
        expected = (
            '-pvalue+%s.EnableVectors=1 -pvalue+%s.Width=64'
            % (SEQUENTIAL, SEQUENTIAL)
        )
        self.assertIn('set pvalues {%s}' % expected, tcl)
        self.assertIn('elaborate -sva $top -vcs $pvalues', tcl)
        self.assertIn('elaborate -cov all -sva $top -vcs $pvalues', tcl)
        self.assertIn(
            'elaborate -cov all -sva $top -vcs "$pvalues -cm_libs vy"', tcl)

    def test_absent_overrides_keep_the_plain_elaborate(self):
        root = build_tree(SEQUENTIAL)
        with working_directory(root), \
                environment(DUT_ROOT=SARGANTANA, SVAPSHOT_ROOT=root), quiet():
            assert main.generate_fpv_vcf_tcl(rtl(SEQUENTIAL), 'sequential')
        tcl = read(os.path.join(root, 'ft_' + SEQUENTIAL, 'FPV_vcf.tcl'))
        self.assertNotIn('set pvalues', tcl)
        self.assertIn('elaborate -sva $top', tcl)
        self.assertNotIn('elaborate -sva $top -vcs $pvalues', tcl)


def verilog_tree(module='generic_fifo_sc_a', extension='.v'):
    """An ft_<module>/ tree as the scaffolder leaves one for a Verilog DUT.

    the scaffolder keeps a Verilog top out of files.vc on purpose — JasperGold reads it
    through a second ``analyze -v2k`` — so the file list this builds is missing
    the design, which is what the VC Formal file list has to notice.
    """
    root = tempfile.mkdtemp(prefix='verilog_tree_', dir=_workspace())
    dut = os.path.join(root, module + extension)
    with open(dut, 'w') as handle:
        handle.write('module %s(clk, rst, do);\ninput clk, rst;\noutput do;\n'
                     'endmodule\n' % module)
    ft_dir = os.path.join(root, 'ft_' + module)
    sva_dir = os.path.join(ft_dir, 'sva')
    os.makedirs(sva_dir)
    for name in (module + '_prop.sv', module + '_bind.svh'):
        with open(os.path.join(sva_dir, name), 'w') as handle:
            handle.write('// placeholder\n')
    manual_sub = os.path.join(ft_dir, 'manual_sub.vc')
    with open(manual_sub, 'w') as handle:
        handle.write('// package and dependency file list\n')
    with open(os.path.join(ft_dir, 'FPV.tcl'), 'w') as handle:
        handle.write('set DUT_PATH %s\n'
                     'set PROP_PATH %s\n'
                     'clock clk\n'
                     'reset -expression (!rst)\n' % (root, sva_dir))
    with open(os.path.join(ft_dir, 'files.vc'), 'w') as handle:
        handle.write('+libext+.v\n+libext+.sv\n'
                     '+incdir+${DUT_PATH}\n'
                     '-y ${DUT_PATH}\n'
                     '-f %s\n'
                     '${PROP_PATH}/%s_prop.sv\n'
                     '${PROP_PATH}/%s_bind.svh\n' %
                     (manual_sub, module, module))
    return root, dut, ft_dir


class TestFileListLanguageSelection(unittest.TestCase):
    """One language for the whole file list cannot hold a Verilog design.

    The checker is SystemVerilog whatever the DUT is, so the two have to be
    compiled as different languages in the same run. ``analyze -format
    sverilog`` on everything works only until the design uses a word
    SystemVerilog reserved later: the OpenCores RAM has a port named ``do``.
    """

    @classmethod
    def setUpClass(cls):
        root, dut, ft_dir = verilog_tree()
        with working_directory(root), quiet():
            cls.ok = main.generate_fpv_vcf_tcl(dut, 'sequential')
        cls.dut = dut
        cls.vc_text = read(os.path.join(ft_dir, 'files_vcf.vc'))

    def test_the_step_succeeds(self):
        self.assertTrue(self.ok)

    def test_verilog_sources_are_compiled_as_verilog(self):
        self.assertIn('+verilog2001ext+.v', self.vc_text)

    def test_the_checker_is_still_compiled_as_systemverilog(self):
        self.assertIn('+systemverilogext+.sv', self.vc_text)
        self.assertIn('+systemverilogext+.svh', self.vc_text)

    def test_the_verilog_dut_is_named_in_the_file_list(self):
        # Left out of files.vc by the scaffolder, and VC Formal has no second analyze
        # command to pick it up: without this the design is compiled only if
        # library search happens to match the file name to the module name.
        self.assertIn(self.dut, self.vc_text)

    def test_the_language_flags_come_before_the_sources(self):
        first_source = self.vc_text.index(self.dut)
        self.assertLess(self.vc_text.index('+verilog2001ext+.v'), first_source)

    def test_dependency_file_lists_come_before_the_dut(self):
        self.assertLess(self.vc_text.index('-f '), self.vc_text.index(self.dut))

    def test_the_dut_comes_before_the_generated_checker(self):
        self.assertLess(
            self.vc_text.index(self.dut),
            self.vc_text.index('generic_fifo_sc_a_prop.sv'),
        )

    def test_the_include_directories_still_come_first(self):
        self.assertTrue(self.vc_text.startswith('+incdir+'))


class TestFileListLanguageSelectionChoices(unittest.TestCase):
    """The DUT's language is a property of the design, not of the tool."""

    def build(self, extension='.v', **kwargs):
        root, dut, ft_dir = verilog_tree(extension=extension)
        with working_directory(root), quiet():
            main.generate_fpv_vcf_tcl(dut, 'sequential', **kwargs)
        return dut, read(os.path.join(ft_dir, 'files_vcf.vc'))

    def test_a_ninety_five_design_can_ask_for_the_earlier_standard(self):
        # Verilog-2001 reserved words a 1995 design may have used as
        # identifiers, so the later standard cannot be the only option.
        _, vc_text = self.build(verilog_std='1995')
        self.assertIn('+verilog1995ext+.v', vc_text)
        self.assertNotIn('+verilog2001ext+.v', vc_text)

    def test_the_later_standard_is_the_default(self):
        _, vc_text = self.build()
        self.assertIn('+verilog2001ext+.v', vc_text)

    def test_a_systemverilog_dut_is_not_listed_twice(self):
        # files.vc already names a .sv top; adding it again would compile the
        # module twice and elaborate to a duplicate definition.
        dut, vc_text = self.build(extension='.sv')
        self.assertEqual(vc_text.count(dut), 1)

    def test_the_flags_are_the_same_whichever_dut_it_is(self):
        # The checker is SystemVerilog either way, so the per-extension
        # selection is not conditional on the DUT.
        _, verilog = self.build(extension='.v')
        _, systemverilog = self.build(extension='.sv')
        for flag in ('+verilog2001ext+.v', '+systemverilogext+.sv'):
            with self.subTest(flag=flag):
                self.assertIn(flag, verilog)
                self.assertIn(flag, systemverilog)


class TestProofBudget(unittest.TestCase):
    """A run has to come back with something to qualify.

    VC Formal's defaults are twelve hours per blocking check command and no
    per-property limit. On fpnew_top — 267k flops, 135 constraint goals —
    `check_constraints -block` runs first and `check_fv` never gets to write a
    PROP_I_RESULT line, so the flow waits all day and learns nothing. A property
    that runs out of time is reported inconclusive, which the qualification
    pipeline already reads as absence of evidence.
    """

    @classmethod
    def setUpClass(cls):
        root, dut, ft_dir = verilog_tree()
        with working_directory(root), quiet():
            main.generate_fpv_vcf_tcl(dut, 'sequential')
        cls.tcl = read(os.path.join(ft_dir, 'FPV_vcf.tcl'))

    def test_both_limits_are_set(self):
        self.assertIn('set_fml_var fml_max_time', self.tcl)
        self.assertIn('set_fml_var fml_property_time_limit', self.tcl)

    def test_the_limits_are_in_force_before_the_first_check(self):
        self.assertLess(self.tcl.index('set_fml_var fml_max_time'),
                        self.tcl.index('check_constraints'))

    def test_a_longer_run_needs_no_regenerated_script(self):
        for variable in ('SVAPSHOT_FML_MAX_TIME', 'SVAPSHOT_FML_PROPERTY_TIME'):
            with self.subTest(variable=variable):
                self.assertIn(f'env({variable})', self.tcl)

    def test_the_constraint_check_is_skipped_when_there_is_nothing_to_check(self):
        # An empty assumption set cannot conflict with itself, and the check is
        # a formal run of its own: on fpnew_top it opened 135 goals ahead of
        # every proof to establish something already known.
        self.assertIn('get_props -usage assume', self.tcl)
        guard = self.tcl.index('if {$assume_count - $script_assume_count > 0} {')
        self.assertGreater(self.tcl.index('check_constraints'), guard)
        self.assertGreater(self.tcl.index('constraints.rpt'), guard)

    def test_reduced_constraint_analysis_uses_the_same_user_assumption_guard(self):
        command = self.tcl.index('compute_reduced_constraints -block')
        guard = self.tcl.rfind(
            'if {$assume_count - $script_assume_count > 0} {', 0, command)
        self.assertNotEqual(guard, -1)
        self.assertIn('REDUCED_CONSTRAINTS_SKIPPED', self.tcl[guard:])

    def test_the_scripts_own_constraints_do_not_count_as_assumptions(self):
        # create_reset adds rst==1 as a constconstraint of class `script`, so a
        # plain count of assume-usage properties is never zero and the check
        # would never be skipped. Subtracting them beats a threshold: a
        # combinational module has no reset constraint to allow for, and a
        # multi-clock one has more than one.
        self.assertIn('get_props -usage assume -class script', self.tcl)

    def test_the_skip_is_recorded_rather_than_silent(self):
        self.assertIn('CHECK_CONSTRAINTS_SKIPPED', self.tcl)

    def test_the_property_set_is_counted_at_run_time(self):
        # The agent rewrites the property file between iterations, so a decision
        # taken when the script was generated would go stale the moment an
        # assumption was added.
        self.assertIn('set assume_count [sizeof_collection', self.tcl)

    def test_a_query_the_tool_rejects_runs_the_check_rather_than_skipping_it(self):
        # Both counts start at zero and are set inside `catch`. If the tool
        # rejects the -class query, script_assume_count stays 0, the difference
        # is the full count and the check runs — the safe direction.
        for variable in ('assume_count', 'script_assume_count'):
            with self.subTest(variable=variable):
                self.assertIn(f'set {variable} 0\n', self.tcl)
                self.assertIn(f'catch {{set {variable} [sizeof_collection', self.tcl)

    def test_setting_a_limit_cannot_abort_the_run(self):
        # An older VC Formal that does not know the variable must degrade to
        # its default rather than take the reports down with it.
        for line in self.tcl.splitlines():
            if 'set_fml_var fml_max_time' in line or 'fml_property_time_limit' in line:
                self.assertTrue(line.strip().startswith('catch {'), line)


if __name__ == '__main__':
    unittest.main(verbosity=2)
