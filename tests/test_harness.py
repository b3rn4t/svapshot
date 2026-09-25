#!/usr/bin/env python3
"""Contract tests for the native formal-harness scaffolder.

The scaffolder is the first stage of the flow, and everything after it inherits
what it produces: the LLM fills in the property file it lays out, SVALint reads
that file, the formal tool runs the TCL script and file list it writes, and the
snapshot is qualified against the top module it elaborates. A port mirrored
wrongly or a TCL pointed at the wrong top surfaces much later as what looks like
a property defect, which is the expensive way to find it.

It is driven in-process through ``scaffold.scaffold_harness``, the same path
``main.run_scaffold`` uses. The modules under test come from Sargantana's
execution stage and cover the cases that matter: a small sequential module, a
combinational one, a wide sequential one, and a clockless module.

    python3 tests/test_harness.py
    python3 tests/test_harness.py TestClockAndReset

Tests marked as expected failures record defects that are real but not yet
fixed; if one starts passing, unittest reports an unexpected success and the
gap can be closed here.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS_DIR)
import _path_setup  # noqa: F401, E402

import coi  # noqa: E402  (needs REPO on the path)
import rtl_clocking  # noqa: E402
from scaffold import ScaffoldError, scaffold_harness  # noqa: E402

SARGANTANA = os.path.join(REPO, 'benchmarks', 'sargantana')
RTL_ROOT = os.path.join(SARGANTANA, 'rtl')
EXE_STAGE = 'rtl/datapath/rtl/exe_stage/rtl'

#: A small sequential module: clock, negative reset, one output.
SEQUENTIAL = 'div_unit'
#: Purely combinational: checker-local formal_clk, no DUT clock.
COMBINATIONAL = 'branch_unit'
#: 52 ports across 20 inputs and 32 outputs: the mirror has to scale.
WIDE = 'exe_stage'
#: No clock and no reset anywhere in its interface.
CLOCKLESS = 'div_4bits'
#: Declares clk, clk_div, clk_div_valid and clk_out: four clock-looking ports,
#: only the first of which is a clock.
MULTI_CLOCK = 'clock_divider_counter'

#: Modules that do not sit directly in the execution stage directory.
_SUBDIRECTORY = {MULTI_CLOCK: 'fpu/src/common_cells/src/deprecated'}


def source_argument(module: str) -> str:
    """The -f value for a module, relative to DUT_ROOT."""
    return '/'.join(part for part in
                    (EXE_STAGE, _SUBDIRECTORY.get(module, ''), module + '.sv') if part)

_WORKSPACE = None


def _workspace() -> str:
    global _WORKSPACE
    if _WORKSPACE is None:
        _WORKSPACE = tempfile.mkdtemp(prefix='harness_contract_')
    return _WORKSPACE


def tearDownModule():
    if _WORKSPACE and os.path.isdir(_WORKSPACE):
        shutil.rmtree(_WORKSPACE, ignore_errors=True)


requires_sargantana = unittest.skipUnless(
    os.path.isdir(SARGANTANA),
    'needs the sargantana tree')


@dataclass
class Generated:
    """The tree the scaffolder left behind for one module."""

    module: str
    module_type: str
    root: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def ft_dir(self) -> str:
        return os.path.join(self.root, 'ft_' + self.module)

    @property
    def sva_dir(self) -> str:
        return os.path.join(self.ft_dir, 'sva')

    def path(self, *parts: str) -> str:
        return os.path.join(self.ft_dir, *parts)

    def _read(self, *parts: str) -> str:
        with open(self.path(*parts), encoding='utf-8', errors='replace') as handle:
            return handle.read()

    @property
    def prop_text(self) -> str:
        return self._read('sva', self.module + '_prop.sv')

    @property
    def bind_text(self) -> str:
        return self._read('sva', self.module + '_bind.svh')

    @property
    def vc_text(self) -> str:
        return self._read('files.vc')

    @property
    def tcl_text(self) -> str:
        return self._read('FPV.tcl')


def rtl_source(module: str) -> str:
    with open(os.path.join(SARGANTANA, source_argument(module)),
              encoding='utf-8', errors='replace') as handle:
        return handle.read()


def invoke(module: str, module_type: str, root: str, env_overrides=None) -> Generated:
    """Run the native scaffolder the way main.py does, under ``root``."""
    import contextlib
    import io

    env = dict(os.environ)
    env['DUT_ROOT'] = SARGANTANA
    env['SVAPSHOT_ROOT'] = root
    for name, value in (env_overrides or {}).items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value

    stdout = io.StringIO()
    stderr = io.StringIO()
    returncode = 0
    saved = {key: os.environ.get(key) for key in ('DUT_ROOT', 'SVAPSHOT_ROOT')}
    try:
        for key in ('DUT_ROOT', 'SVAPSHOT_ROOT'):
            if key in env:
                os.environ[key] = env[key]
            else:
                os.environ.pop(key, None)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            scaffold_harness(
                filename=source_argument(module),
                sources=[RTL_ROOT],
                module_type=module_type,
                dut_root=env.get('DUT_ROOT'),
                output_root=env.get('SVAPSHOT_ROOT'),
                work_dir=root,
            )
    except ScaffoldError as exc:
        returncode = exc.returncode
        stderr.write(exc.message + '\n')
    except Exception as exc:
        returncode = 1
        stderr.write(str(exc) + '\n')
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return Generated(module, module_type, root, returncode,
                     stdout.getvalue(), stderr.getvalue())


@functools.lru_cache(maxsize=None)
def generate(module: str, module_type: str = 'sequential') -> Generated:
    """Generate once per (module, type); every test reads the same tree."""
    root = os.path.join(_workspace(), '{}-{}'.format(module, module_type))
    os.makedirs(root, exist_ok=True)
    return invoke(module, module_type, root)


def property_ports(generated: Generated) -> set:
    """Port names declared by the generated property module."""
    graph = coi.build_signal_graph(generated.prop_text, generated.module + '_prop')
    return graph.ports_in | graph.ports_out


def rtl_ports(module: str):
    graph = coi.build_signal_graph(rtl_source(module), module)
    return graph.ports_in, graph.ports_out


def port_declarations(generated: Generated) -> dict:
    """Map each port of the generated module to the line that declares it.

    The header is located with a balanced-paren scan rather than by searching
    for the first ``);``, because the licence banner the scaffolder copies from the RTL
    contains ``(the "License");``.
    """
    _, _, port_list, _ = coi.split_module_header(generated.prop_text)
    declarations = {}
    for line in port_list.splitlines():
        code = line.split('//')[0].strip()
        if not code or code.startswith('`'):
            continue  # a compiler directive guarding conditional ports
        names = re.findall(r'[A-Za-z_]\w*', code)
        if names:
            declarations[names[-1]] = line.strip()
    return declarations


@requires_sargantana
class TestGeneratedTree(unittest.TestCase):
    """The directory skeleton every later stage writes into."""

    def test_the_run_succeeds(self):
        run = generate(SEQUENTIAL)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_creates_the_ft_and_sva_directories(self):
        run = generate(SEQUENTIAL)
        self.assertTrue(os.path.isdir(run.ft_dir), run.ft_dir)
        self.assertTrue(os.path.isdir(run.sva_dir), run.sva_dir)

    def test_writes_every_artifact_the_pipeline_expects(self):
        run = generate(SEQUENTIAL)
        for relative in ('FPV.tcl', 'files.vc', 'manual_sub.vc',
                         os.path.join('sva', SEQUENTIAL + '_prop.sv'),
                         os.path.join('sva', SEQUENTIAL + '_bind.svh')):
            with self.subTest(artifact=relative):
                path = run.path(relative)
                self.assertTrue(os.path.isfile(path), path)
                self.assertGreater(os.path.getsize(path), 0, relative)

    def test_the_tree_is_named_after_the_module(self):
        run = generate(WIDE)
        self.assertEqual(os.path.basename(run.ft_dir), 'ft_' + WIDE)

    def test_nothing_is_written_outside_the_ft_directory(self):
        # The working directory is shared with the rest of the flow, so a stray
        # file here would land in the user's repository root.
        run = generate(COMBINATIONAL, 'combinational')
        self.assertEqual(sorted(os.listdir(run.root)), ['ft_' + COMBINATIONAL])


@requires_sargantana
class TestPropertyModuleInterface(unittest.TestCase):
    """The property module has to mirror the DUT interface exactly."""

    def test_every_rtl_port_is_mirrored(self):
        for module, module_type in ((SEQUENTIAL, 'sequential'),
                                    (COMBINATIONAL, 'combinational'),
                                    (WIDE, 'sequential')):
            with self.subTest(module=module):
                inputs, outputs = rtl_ports(module)
                mirrored = property_ports(generate(module, module_type))
                self.assertEqual(inputs | outputs, mirrored)

    def test_the_wide_module_keeps_all_of_its_ports(self):
        # exe_stage has 52; a truncated mirror would still elaborate and only
        # show up as properties that cannot reference the signal they need.
        inputs, outputs = rtl_ports(WIDE)
        self.assertEqual(len(inputs) + len(outputs), 52)
        self.assertEqual(len(property_ports(generate(WIDE))), 52)

    def test_every_port_is_declared_as_an_input(self):
        # The property module observes the DUT; a mirrored output declared as
        # an output would drive it instead.
        for module in (SEQUENTIAL, WIDE):
            declarations = port_declarations(generate(module))
            inputs, outputs = rtl_ports(module)
            self.assertEqual(set(declarations), inputs | outputs)
            for name, declaration in declarations.items():
                with self.subTest(module=module, port=name):
                    self.assertRegex(declaration, r'^input\b')

    def test_mirrored_outputs_are_marked_as_such(self):
        # The marker is how a reader (and the LLM prompt) tells which mirrored
        # signals the DUT drives.
        for module in (SEQUENTIAL, WIDE):
            declarations = port_declarations(generate(module))
            inputs, outputs = rtl_ports(module)
            for name in outputs:
                with self.subTest(module=module, port=name):
                    self.assertIn('//output', declarations[name])
            for name in inputs:
                with self.subTest(module=module, port=name):
                    self.assertNotIn('//output', declarations[name])

    def test_conditional_ports_keep_their_guard(self):
        # exe_stage declares store_addr_o and store_data_o under
        # `ifdef SIM_COMMIT_LOG. Mirroring them unguarded would break every
        # elaboration that leaves the macro undefined.
        header = coi.split_module_header(generate(WIDE).prop_text)[2]
        guarded = re.search(
            r'`ifdef\s+SIM_COMMIT_LOG(?P<ports>.*?)`endif', header, re.DOTALL)
        self.assertIsNotNone(guarded, 'the `ifdef guard was dropped')
        self.assertIn('store_addr_o', guarded.group('ports'))
        self.assertIn('store_data_o', guarded.group('ports'))

    def test_the_module_is_named_after_the_dut(self):
        run = generate(SEQUENTIAL)
        self.assertRegex(run.prop_text, r'\bmodule\s+%s_prop\b' % SEQUENTIAL)
        self.assertIn('endmodule', run.prop_text)

    def test_assert_inputs_is_a_parameter(self):
        run = generate(SEQUENTIAL)
        self.assertRegex(run.prop_text, r'parameter\s+ASSERT_INPUTS\s*=\s*0')

    def test_the_designer_section_marker_is_present(self):
        # The agent appends generated assertions below this marker, and
        # regeneration preserves whatever sits under it.
        self.assertIn('//====DESIGNER-ADDED-SVA====//', generate(SEQUENTIAL).prop_text)


class TestCombinationalClockingRestore(unittest.TestCase):
    """VC Formal NCIFA if a later rewrite drops the checker-local clock."""

    def test_a_stripped_shell_gets_formal_clk_back(self):
        stripped = (
            'module alu_core_prop;\n'
            'genvar j;\n'
            '\n'
            '// Re-defined wires\n'
            '//====DESIGNER-ADDED-SVA====//\n'
        )
        restored = rtl_clocking.ensure_combinational_clocking(stripped)
        self.assertIn('logic formal_clk;', restored)
        self.assertIn('default clocking cb @(posedge formal_clk);', restored)
        again = rtl_clocking.ensure_combinational_clocking(restored)
        self.assertEqual(again.count('logic formal_clk;'), 1)
        self.assertEqual(again.count('default clocking'), 1)


@requires_sargantana
class TestClockAndReset(unittest.TestCase):
    """Clocking must come from the module's own interface, never invented."""

    def test_sequential_clocking_uses_the_module_clock(self):
        run = generate(SEQUENTIAL)
        self.assertIn('default clocking cb @(posedge clk_i);', run.prop_text)
        self.assertIn('default disable iff (!rstn_i);', run.prop_text)

    def test_the_clock_and_reset_are_ports_of_the_property_module(self):
        # Without this the property file cannot elaborate: the clocking block
        # would reference an identifier that the module never declares.
        run = generate(SEQUENTIAL)
        declared = property_ports(run)
        clock = re.search(r'default clocking cb @\(posedge (\w+)\)', run.prop_text)
        reset = re.search(r'default disable iff \(!(\w+)\)', run.prop_text)
        self.assertIsNotNone(clock)
        self.assertIsNotNone(reset)
        self.assertIn(clock.group(1), declared)
        self.assertIn(reset.group(1), declared)

    def test_a_combinational_module_clocks_on_formal_clk(self):
        run = generate(COMBINATIONAL, 'combinational')
        self.assertIn('default clocking cb @(posedge formal_clk);', run.prop_text)
        self.assertNotIn('disable iff', run.prop_text)
        self.assertNotIn('formal_clk', property_ports(run))

    def test_the_tcl_clock_matches_the_property_file(self):
        run = generate(SEQUENTIAL)
        self.assertIn('clock clk_i', run.tcl_text)
        self.assertIn('reset -expression (!rstn_i)', run.tcl_text)

    def test_a_combinational_run_declares_no_clock_to_the_tool(self):
        run = generate(COMBINATIONAL, 'combinational')
        self.assertIn('clock -none', run.tcl_text)
        self.assertIn('reset -none', run.tcl_text)


@requires_sargantana
class TestClockChoiceAmongLookalikes(unittest.TestCase):
    """Several ports can look like clocks; the RTL says which one is.

    clock_divider_counter declares clk, clk_div, clk_div_valid and clk_out. Its
    `always_ff @(posedge clk, negedge rstn)` settles all of it: the clock, that
    the reset is asynchronous, and that it is active low. The port names settle
    none of it, and the last match used to win, which clocked the property
    module on clk_out, an output the DUT drives.
    """

    @classmethod
    def setUpClass(cls):
        cls.divider = generate(MULTI_CLOCK)
        cls.clocking = rtl_clocking.analyze_clocking(rtl_source(MULTI_CLOCK))

    def test_the_clock_is_the_one_the_registers_run_on(self):
        self.assertEqual(self.clocking.clock, 'clk')
        self.assertIn('default clocking cb @(posedge clk);', self.divider.prop_text)

    def test_a_driven_output_is_never_the_clock(self):
        clock = re.search(r'default clocking cb @\(posedge (\w+)\)', self.divider.prop_text)
        _, outputs = rtl_ports(MULTI_CLOCK)
        self.assertIn('clk_out', outputs)
        self.assertNotIn(clock.group(1), outputs)

    def test_the_tool_script_agrees_with_the_property_file(self):
        self.assertIn('clock %s\n' % self.clocking.clock, self.divider.tcl_text)

    def test_the_reset_polarity_comes_from_the_sensitivity_list(self):
        # negedge rstn is a statement of polarity, where the name is a hint.
        self.assertTrue(self.clocking.reset_active_low)
        self.assertIn('default disable iff (!rstn);', self.divider.prop_text)
        self.assertIn('reset -expression (!rstn)', self.divider.tcl_text)

    def test_both_stages_answer_from_the_same_analysis(self):
        # The scaffolder writes the clocking block and main.py decides the module type;
        # if they used different rules one would build a testbench the other
        # thinks is wrong.
        for module in (SEQUENTIAL, MULTI_CLOCK):
            with self.subTest(module=module):
                clocking = rtl_clocking.analyze_clocking(rtl_source(module))
                self.assertIn('default clocking cb @(%s %s);'
                              % (clocking.clock_edge, clocking.clock),
                              generate(module).prop_text)


@requires_sargantana
class TestBindFile(unittest.TestCase):
    """The bind is what attaches the property module to the DUT instance."""

    def test_binds_the_dut_to_its_property_module(self):
        run = generate(SEQUENTIAL)
        self.assertRegex(run.bind_text,
                         r'bind\s+%s\s+%s_prop' % (SEQUENTIAL, SEQUENTIAL))

    def test_connects_by_name(self):
        # `.*` is what keeps the bind correct as the interface changes.
        self.assertIn('.*', generate(SEQUENTIAL).bind_text)

    def test_input_assertions_are_off_by_default(self):
        # ASSERT_INPUTS(1) would constrain the environment rather than check
        # the DUT, so the default has to be 0.
        self.assertRegex(generate(SEQUENTIAL).bind_text,
                         r'\.ASSERT_INPUTS\s*\(\s*0\s*\)')


@requires_sargantana
class TestFileList(unittest.TestCase):
    """files.vc is the only description of the design the tool receives."""

    def test_the_dut_source_is_listed_under_dut_root(self):
        run = generate(SEQUENTIAL)
        self.assertIn('${DUT_ROOT}/%s/%s.sv' % (EXE_STAGE, SEQUENTIAL), run.vc_text)

    def test_the_property_and_bind_files_are_listed(self):
        run = generate(SEQUENTIAL)
        self.assertIn('${PROP_PATH}/%s_prop.sv' % SEQUENTIAL, run.vc_text)
        self.assertIn('${PROP_PATH}/%s_bind.svh' % SEQUENTIAL, run.vc_text)

    def test_the_manual_dependency_file_is_included(self):
        self.assertIn('manual_sub.vc', generate(SEQUENTIAL).vc_text)

    def test_the_source_tree_is_on_the_library_path(self):
        run = generate(SEQUENTIAL)
        self.assertIn('-y %s' % os.path.join(SARGANTANA, EXE_STAGE), run.vc_text)

    def test_the_whole_source_tree_is_added_including_testbenches(self):
        # Characterisation, not endorsement: every subdirectory of -src becomes
        # a library directory, so documentation, CI and testbench folders are
        # searched too. Sargantana's tb/ trees define tb_module six times over,
        # which only stays harmless because no design module instantiates it.
        # If the walk is ever narrowed to RTL directories, update this test
        # deliberately rather than letting the change pass unnoticed.
        library_dirs = re.findall(r'^-y\s+(\S+)$', generate(SEQUENTIAL).vc_text,
                                  re.MULTILINE)
        self.assertTrue(any(re.search(r'/tb(/|$)', d) for d in library_dirs))
        self.assertTrue(any(d.endswith('/doc') for d in library_dirs))

    def test_mutation_trees_are_not_on_the_library_path(self):
        # AssertLLM ships mutations/ and buggy_artifacts/ beside the golden
        # RTL with the same module names. Searching them lets VCS elaborate
        # a mutant instead of the design under test.
        library_dirs = re.findall(r'^-y\s+(\S+)$', generate(SEQUENTIAL).vc_text,
                                  re.MULTILINE)
        for path in library_dirs:
            with self.subTest(path=path):
                self.assertNotRegex(path, r'(^|/)(mutations|buggy_artifacts)(/|$)')


@requires_sargantana
class TestNonAnsiVerilogDut(unittest.TestCase):
    """A Verilog-95 OpenCores module must yield a compilable SV checker.

    AssertLLM's i2c_master_top puts ``);`` after the last bare port name and
    declares directions in the body. The old parser never closed the port list,
    copied ``always``/instances into the property module, and failed VCS.
    """

    I2C = os.path.join(
        REPO, 'benchmarks', 'AssertLLM2', 'designs',
        'COMMUNICATION_CONTROLLER', 'i2c')

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(os.path.join(cls.I2C, 'i2c_master_top.v')):
            raise unittest.SkipTest('AssertLLM I2C design is not present')
        cls.root = os.path.join(_workspace(), 'i2c-nonansi')
        os.makedirs(cls.root, exist_ok=True)
        import contextlib
        import io
        stdout = io.StringIO()
        stderr = io.StringIO()
        returncode = 0
        saved = {key: os.environ.get(key) for key in ('DUT_ROOT', 'SVAPSHOT_ROOT')}
        try:
            os.environ['DUT_ROOT'] = cls.I2C
            os.environ['SVAPSHOT_ROOT'] = cls.root
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                scaffold_harness(
                    filename='i2c_master_top.v',
                    sources=[cls.I2C, os.path.join(cls.I2C, 'include')],
                    include=os.path.join(cls.I2C, 'include'),
                    module_type='sequential',
                    dut_root=cls.I2C,
                    output_root=cls.root,
                    work_dir=cls.root,
                )
        except ScaffoldError as exc:
            returncode = exc.returncode
            stderr.write(exc.message + '\n')
        except Exception as exc:
            returncode = 1
            stderr.write(str(exc) + '\n')
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        cls.generated = Generated('i2c_master_top', 'sequential', cls.root,
                                  returncode, stdout.getvalue(), stderr.getvalue())

    def test_scaffold_succeeds(self):
        self.assertEqual(self.generated.returncode, 0, self.generated.stderr)

    def test_every_dut_port_is_mirrored_as_an_input(self):
        with open(os.path.join(self.I2C, 'i2c_master_top.v'),
                  encoding='utf-8', errors='replace') as handle:
            rtl = coi.build_signal_graph(handle.read(), 'i2c_master_top',
                                         'verilog')
        prop_ports = property_ports(self.generated)
        self.assertEqual(prop_ports, rtl.ports_in | rtl.ports_out)
        for name, declaration in port_declarations(self.generated).items():
            with self.subTest(port=name):
                self.assertRegex(declaration, r'^input\b')

    def test_the_rtl_body_is_not_copied_into_the_checker(self):
        self.assertNotIn('always @(posedge', self.generated.prop_text)
        self.assertNotIn('i2c_master_byte_ctrl', self.generated.prop_text)
        self.assertNotIn('`include', self.generated.prop_text)

    def test_clock_and_reset_are_primary_inputs(self):
        self.assertIn('default clocking cb @(posedge wb_clk_i);',
                      self.generated.prop_text)
        self.assertIn('default disable iff (!arst_n);', self.generated.prop_text)
        declared = property_ports(self.generated)
        self.assertIn('wb_clk_i', declared)
        self.assertIn('arst_n', declared)
        self.assertNotIn('rst_n', declared)

    def test_mutation_directories_are_not_searched(self):
        library_dirs = re.findall(r'^-y\s+(\S+)$', self.generated.vc_text,
                                  re.MULTILINE)
        for path in library_dirs:
            self.assertNotRegex(path, r'(^|/)(mutations|buggy_artifacts)(/|$)')

    def test_the_tool_script_resets_a_port(self):
        self.assertIn('clock wb_clk_i', self.generated.tcl_text)
        self.assertIn('reset -expression (!arst_n)', self.generated.tcl_text)


@requires_sargantana
class TestTclScript(unittest.TestCase):
    """FPV.tcl has to be runnable from the environment main.py sets up."""

    def test_paths_come_from_the_environment(self):
        run = generate(SEQUENTIAL)
        self.assertIn('set SVAPSHOT_ROOT $env(SVAPSHOT_ROOT)', run.tcl_text)
        self.assertIn('set DUT_ROOT $env(DUT_ROOT)', run.tcl_text)

    def test_the_property_path_points_at_the_sva_directory(self):
        run = generate(SEQUENTIAL)
        self.assertIn('set PROP_PATH ${SVAPSHOT_ROOT}/ft_%s/sva' % SEQUENTIAL,
                      run.tcl_text)

    def test_it_analyses_the_generated_file_list(self):
        run = generate(SEQUENTIAL)
        self.assertIn('-f ${SVAPSHOT_ROOT}/ft_%s/files.vc' % SEQUENTIAL, run.tcl_text)

    def test_it_elaborates_the_dut_as_top(self):
        for module in (SEQUENTIAL, WIDE):
            with self.subTest(module=module):
                self.assertIn('elaborate -top %s ' % module,
                              generate(module).tcl_text)


@requires_sargantana
class TestRegeneration(unittest.TestCase):
    """Regeneration must not discard work already done on the property file."""

    @classmethod
    def setUpClass(cls):
        cls.root = os.path.join(_workspace(), 'regeneration')
        os.makedirs(cls.root, exist_ok=True)
        first = invoke(SEQUENTIAL, 'sequential', cls.root)
        assert first.returncode == 0, first.stderr

        cls.handwritten = ('  a_handwritten: assert property (1) '
                           'else $error("kept");')
        path = first.path('sva', SEQUENTIAL + '_prop.sv')
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
        cls.generated_before = text.split('//====DESIGNER-ADDED-SVA====//')[0]
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(text.rstrip('\n').replace(
                'endmodule', cls.handwritten + '\nendmodule'))

        cls.second = invoke(SEQUENTIAL, 'sequential', cls.root)

    def test_the_second_run_succeeds(self):
        self.assertEqual(self.second.returncode, 0, self.second.stderr)

    def test_designer_added_assertions_survive(self):
        self.assertIn(self.handwritten, self.second.prop_text)

    def test_the_previous_property_file_is_backed_up(self):
        self.assertTrue(os.path.isfile(
            self.second.path('sva', SEQUENTIAL + '_prop_old.sv')))

    def test_the_generated_section_is_reproduced_unchanged(self):
        after = self.second.prop_text.split('//====DESIGNER-ADDED-SVA====//')[0]
        self.assertEqual(self.generated_before, after)


@requires_sargantana
class TestEnvironmentGuards(unittest.TestCase):
    """A missing path variable must fail loudly, not write a broken tree."""

    def test_a_missing_dut_root_is_reported(self):
        root = tempfile.mkdtemp(dir=_workspace())
        run = invoke(SEQUENTIAL, 'sequential', root, {'DUT_ROOT': None})
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('DUT_ROOT', run.stdout + run.stderr)

    def test_a_missing_svapshot_root_is_reported(self):
        root = tempfile.mkdtemp(dir=_workspace())
        run = invoke(SEQUENTIAL, 'sequential', root,
                     {'SVAPSHOT_ROOT': None})
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('SVAPSHOT_ROOT', run.stdout + run.stderr)


@requires_sargantana
class TestClocklessModuleIsRefused(unittest.TestCase):
    """A sequential testbench for a module with no clock is not writable.

    The scaffolder used to fall back to the names `clk` and `rst_n`, which the property
    module never declares, producing a file that cannot elaborate. It now stops
    and says so.
    """

    @classmethod
    def setUpClass(cls):
        cls.root = os.path.join(_workspace(), 'clockless')
        os.makedirs(cls.root, exist_ok=True)
        cls.attempt = invoke(CLOCKLESS, 'sequential', cls.root)

    def test_the_run_fails(self):
        self.assertNotEqual(self.attempt.returncode, 0)

    def test_the_reason_names_the_missing_clock(self):
        output = self.attempt.stdout + self.attempt.stderr
        self.assertIn('no clock port', output)
        self.assertIn('-mt combinational', output)

    def test_no_property_file_is_left_behind(self):
        # A half-written file here would be picked up by the next stage as if
        # scaffolding had succeeded.
        self.assertFalse(os.path.isfile(
            self.attempt.path('sva', CLOCKLESS + '_prop.sv')))

    def test_the_same_module_succeeds_as_combinational(self):
        run = generate(CLOCKLESS, 'combinational')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('default clocking cb @(posedge formal_clk);', run.prop_text)
        self.assertEqual(property_ports(run), set().union(*rtl_ports(CLOCKLESS)))


@requires_sargantana
class TestPackageImportsAreLeftToMain(unittest.TestCase):
    """The property module mirrors types without importing their packages.

    This is a handoff, not an omission: main.py owns package discovery and
    injects the imports in its own step, because it is the stage that knows the
    source and include paths. The test pins the handoff so the two stages cannot
    drift apart silently; test_main.py checks the other half.
    """

    def test_the_mirror_uses_package_types(self):
        self.assertIn('rr_exe_arith_instr_t', generate(SEQUENTIAL).prop_text)

    def test_no_imports_are_emitted_here(self):
        _, _, _, body = coi.split_module_header(generate(SEQUENTIAL).prop_text)
        header = generate(SEQUENTIAL).prop_text[:-len(body)] if body else ''
        self.assertNotIn('import ', header)


class TestVerilog95BodyParameters(unittest.TestCase):
    """Verilog-95 body parameters must be on the checker so VCF elaborates.

    FVEval ``counter`` declares ``width``/``min``/``max`` after the port list.
    Copying only ANSI ``#()`` parameters left ``[width-1:0]`` undeclared.
    """

    DUT = (
        "module counter(clk, rst, count);\n"
        "parameter width = 1;\n"
        "parameter min = 0;\n"
        "parameter [width:0] max = ((1<<width)-1);\n"
        "input clk;\n"
        "input rst;\n"
        "input [width-1:0] count;\n"
        "endmodule\n"
    )

    @classmethod
    def setUpClass(cls):
        import contextlib
        import io

        cls.root = tempfile.mkdtemp(prefix='harness_bodyparam_')
        cls.dut_root = os.path.join(cls.root, 'rtl')
        os.makedirs(cls.dut_root, exist_ok=True)
        with open(os.path.join(cls.dut_root, 'counter.sv'), 'w',
                  encoding='utf-8') as handle:
            handle.write(cls.DUT)

        stdout = io.StringIO()
        stderr = io.StringIO()
        returncode = 0
        saved = {key: os.environ.get(key)
                 for key in ('DUT_ROOT', 'SVAPSHOT_ROOT')}
        try:
            os.environ['DUT_ROOT'] = cls.dut_root
            os.environ['SVAPSHOT_ROOT'] = cls.root
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                scaffold_harness(
                    filename='counter.sv',
                    sources=[cls.dut_root],
                    module_type='sequential',
                    dut_root=cls.dut_root,
                    output_root=cls.root,
                    work_dir=cls.root,
                )
        except ScaffoldError as exc:
            returncode = exc.returncode
            stderr.write(exc.message + '\n')
        except Exception as exc:
            returncode = 1
            stderr.write(str(exc) + '\n')
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        cls.generated = Generated(
            'counter', 'sequential', cls.root, returncode,
            stdout.getvalue(), stderr.getvalue())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_scaffold_succeeds(self):
        self.assertEqual(self.generated.returncode, 0, self.generated.stderr)

    def test_body_parameters_are_declared_on_the_checker(self):
        prop = self.generated.prop_text
        self.assertIn('parameter width = 1', prop)
        self.assertIn('parameter min = 0', prop)
        self.assertIn('parameter [width:0] max', prop)
        self.assertLess(prop.index('parameter width'), prop.index('input'))
        self.assertIn('input [width-1:0] count', prop)

    def test_body_parameters_are_bound(self):
        bind = self.generated.bind_text
        self.assertIn('.width (width)', bind)
        self.assertIn('.min (min)', bind)
        self.assertIn('.max (max)', bind)
        self.assertIn('.ASSERT_INPUTS (0)', bind)


class TestFvevalCounterBodyParameters(unittest.TestCase):
    """The DATE FVEval counter DUT is the live compile failure."""

    FVEVAL = os.path.join(
        REPO, 'benchmarks', 'FVEval', 'data_nl2sva', 'annotated_tb')

    @classmethod
    def setUpClass(cls):
        dut = os.path.join(cls.FVEVAL, 'counter.sv')
        if not os.path.isfile(dut):
            raise unittest.SkipTest('FVEval counter DUT is not present')
        import contextlib
        import io

        cls.root = tempfile.mkdtemp(prefix='harness_fveval_')
        stdout = io.StringIO()
        stderr = io.StringIO()
        returncode = 0
        saved = {key: os.environ.get(key)
                 for key in ('DUT_ROOT', 'SVAPSHOT_ROOT')}
        try:
            os.environ['DUT_ROOT'] = cls.FVEVAL
            os.environ['SVAPSHOT_ROOT'] = cls.root
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                scaffold_harness(
                    filename='counter.sv',
                    sources=[cls.FVEVAL],
                    module_type='sequential',
                    dut_root=cls.FVEVAL,
                    output_root=cls.root,
                    work_dir=cls.root,
                )
        except ScaffoldError as exc:
            returncode = exc.returncode
            stderr.write(exc.message + '\n')
        except Exception as exc:
            returncode = 1
            stderr.write(str(exc) + '\n')
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        cls.generated = Generated(
            'counter', 'sequential', cls.root, returncode,
            stdout.getvalue(), stderr.getvalue())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_width_min_max_are_visible_to_vcf(self):
        self.assertEqual(self.generated.returncode, 0, self.generated.stderr)
        prop = self.generated.prop_text
        bind = self.generated.bind_text
        self.assertIn('parameter width = 1', prop)
        self.assertIn('parameter min = 0', prop)
        self.assertIn('parameter [width:0] max', prop)
        self.assertIn('.width (width)', bind)
        self.assertIn('.min (min)', bind)
        self.assertIn('.max (max)', bind)


if __name__ == '__main__':
    unittest.main(verbosity=2)
