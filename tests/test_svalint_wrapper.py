#!/usr/bin/env python3
"""Contract tests for the lint gate before assertion database construction.

The suite exercises a real temporary subprocess for invocation semantics and
feeds representative SVALint output into the pure parser. Infrastructure
failures must fail closed: absence of an ``ERROR`` summary is never evidence
that the property file is lint-clean.
"""

from __future__ import annotations

import os
import importlib.util
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import _path_setup  # noqa: F401, E402

import agent as agent_module
import svalint_wrapper as wrapper
from svalint_wrapper import SVALintResult, SVALintViolation


CLEAN = """\
AsFigo: Summary
ERROR : 0
WARNING : 0
"""

ONE_VIOLATION = """\
AsFigo: Violation: [ASFI-05]:
Assertion label must start with a_.
a_bad: assert property ((req_i |=> gnt_o))
AsFigo: Summary
ERROR : 1
WARNING : 0
"""

TWO_VIOLATIONS = """\
AsFigo: Violation: [ASFI-05]:
Assertion label must start with a_.
a_bad: assert property ((req_i |=> gnt_o))
AsFigo: Violation: [ASFI-12]:
Do not use an explicit clock when default clocking exists.
a_clock: assert property (@(posedge clk_i) req_i)
AsFigo: Summary
ERROR : 2
WARNING : 1
"""

INSTALLED_SVALINT = Path(__file__).resolve().parents[1] / 'SVALint'
REAL_SVALINT_READY = (
    (INSTALLED_SVALINT / 'bin' / 'svalint.py').is_file()
    and importlib.util.find_spec('anytree') is not None
    and importlib.util.find_spec('tomli') is not None
    and wrapper.resolve_verible_executable() is not None
)


def parse(
    output: str,
    *,
    returncode: int = 0,
    stdout: str | None = None,
    stderr: str = '',
) -> SVALintResult:
    actual_stdout = output if stdout is None else stdout
    combined = output if stdout is None else wrapper._combine_output(stdout, stderr)
    return wrapper._parse_svalint_output(
        returncode, actual_stdout, stderr, combined)


def result(
    *,
    returncode: int = 0,
    errors: int = 0,
    warnings: int = 0,
    violations=None,
    passed: bool | None = None,
    execution_error: str = '',
    stdout: str = '',
    stderr: str = '',
) -> SVALintResult:
    return SVALintResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        error_count=errors,
        warning_count=warnings,
        violations=list(violations or []),
        passed=(returncode == 0 and errors == 0)
        if passed is None else passed,
        execution_error=execution_error,
    )


class FakeSVALint:
    """Temporary SVALint checkout with an executable Python entry point."""

    SCRIPT = """\
import os
import pathlib
import sys

args_file = pathlib.Path(os.environ["FAKE_ARGS_FILE"])
args_file.write_text("\\n".join(sys.argv[1:]))
config_file = pathlib.Path(os.environ["FAKE_CONFIG_FILE"])
config_file.write_text(os.environ.get("SVALINT_CONFIG", "<unset>"))

target = pathlib.Path(sys.argv[sys.argv.index("-t") + 1])
mode = target.read_text().strip()
if mode == "clean":
    print("ERROR : 0")
    print("WARNING : 0")
elif mode == "warning":
    print("ERROR : 0")
    print("WARNING : 2")
elif mode == "violation":
    print("AsFigo: Violation: [RULE-1]:")
    print("Bad assertion")
    print("a_bad: assert property ((bad_i))")
    print("ERROR : 1")
    print("WARNING : 0")
    raise SystemExit(1)
elif mode == "stderr":
    print("AsFigo: Violation: [RULE-ERR]:", file=sys.stderr)
    print("Reported on stderr", file=sys.stderr)
    print("a_bad: assert property ((bad_i))", file=sys.stderr)
    print("ERROR : 1", file=sys.stderr)
    print("WARNING : 0", file=sys.stderr)
    raise SystemExit(1)
elif mode == "crash":
    print("internal parser traceback", file=sys.stderr)
    raise SystemExit(7)
elif mode == "sleep":
    import time
    time.sleep(5)
"""

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fake_svalint_')
        base = Path(self.temp.name)
        self.root = base / 'SVALint checkout'
        (self.root / 'bin').mkdir(parents=True)
        script = self.root / 'bin' / 'svalint.py'
        script.write_text(textwrap.dedent(self.SCRIPT), encoding='utf-8')
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self.args_file = base / 'args.txt'
        self.config_capture = base / 'config.txt'
        self.old_env = os.environ.copy()
        os.environ['FAKE_ARGS_FILE'] = str(self.args_file)
        os.environ['FAKE_CONFIG_FILE'] = str(self.config_capture)
        return self

    def target(self, mode: str, name: str = 'property file.sv') -> Path:
        path = Path(self.temp.name) / name
        path.write_text(mode, encoding='utf-8')
        return path

    def __exit__(self, *_args):
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()


class TestLocalSVALintResolution(unittest.TestCase):
    def test_fallback_points_at_vendored_repository_directory(self):
        self.assertEqual(wrapper._LOCAL_SVALINT_ROOT, INSTALLED_SVALINT)
        self.assertEqual(wrapper.DEFAULT_SVALINT_ROOT, INSTALLED_SVALINT)
        self.assertTrue((wrapper._LOCAL_SVALINT_ROOT / 'bin' / 'svalint.py').is_file())
        self.assertNotEqual(str(wrapper.DEFAULT_SVALINT_ROOT), '/opt/SVALint')

    def test_vendored_checkout_wins_over_a_stale_environment(self):
        with mock.patch.dict(os.environ, {'SVALINT_ROOT': '/tmp/missing-SVALint'}):
            self.assertEqual(
                wrapper.resolve_svalint_root().resolve(),
                wrapper._LOCAL_SVALINT_ROOT.resolve(),
            )

    def test_resolution_ignores_cwd_when_the_vendored_checkout_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            old = os.getcwd()
            os.chdir(directory)
            try:
                with mock.patch.dict(os.environ, {'SVALINT_ROOT': '/missing'}):
                    self.assertEqual(
                        wrapper.resolve_svalint_root().resolve(),
                        wrapper._LOCAL_SVALINT_ROOT.resolve(),
                    )
            finally:
                os.chdir(old)

    def test_an_explicit_missing_root_is_not_silently_replaced(self):
        missing = Path('/tmp/missing-SVALint')
        self.assertEqual(wrapper.resolve_svalint_root(missing), missing)

    def test_stripped_path_still_finds_a_local_verible(self):
        extras = (
            Path.home() / '.local' / 'bin' / 'verible-verilog-syntax',
            Path('/usr/local/bin/verible-verilog-syntax'),
            Path('/usr/bin/verible-verilog-syntax'),
        )
        if not any(path.is_file() for path in extras):
            self.skipTest('no well-known verible-verilog-syntax location')
        with mock.patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}, clear=False):
            found = wrapper.resolve_verible_executable()
        self.assertIsNotNone(found)
        self.assertEqual(found.name, 'verible-verilog-syntax')
        self.assertTrue(found.is_file())

    def test_run_svalint_injects_verible_into_the_subprocess_path(self):
        verible = Path('/opt/verible/verible-verilog-syntax')
        with FakeSVALint() as fake:
            captured = {}
            real_run = wrapper.subprocess.run

            def spy(*args, **kwargs):
                captured['env'] = kwargs.get('env')
                return real_run(*args, **kwargs)

            with mock.patch.object(
                    wrapper, 'resolve_verible_executable', return_value=verible):
                with mock.patch.object(wrapper.subprocess, 'run', side_effect=spy):
                    wrapper.run_svalint(
                        fake.target('clean'), svalint_root=fake.root)
        self.assertIn(
            '/opt/verible', captured['env']['PATH'].split(os.pathsep))


class TestRealSubprocessInvocation(unittest.TestCase):
    def test_a_clean_file_passes(self):
        with FakeSVALint() as fake:
            parsed = wrapper.run_svalint(
                fake.target('clean'), svalint_root=fake.root)
        self.assertTrue(parsed.passed)
        self.assertEqual((parsed.error_count, parsed.warning_count), (0, 0))

    def test_warnings_do_not_trigger_an_llm_syntax_iteration(self):
        with FakeSVALint() as fake:
            parsed = wrapper.run_svalint(
                fake.target('warning'), svalint_root=fake.root)
        self.assertTrue(parsed.passed)
        self.assertEqual(parsed.warning_count, 2)

    def test_a_lint_violation_fails_and_is_structured(self):
        with FakeSVALint() as fake:
            parsed = wrapper.run_svalint(
                fake.target('violation'), svalint_root=fake.root)
        self.assertFalse(parsed.passed)
        self.assertEqual(parsed.error_count, 1)
        self.assertEqual(parsed.violations[0].rule_id, 'RULE-1')
        self.assertEqual(
            parsed.violations[0].assertion_text,
            'a_bad: assert property ((bad_i))',
        )

    def test_violations_written_to_stderr_are_not_lost(self):
        with FakeSVALint() as fake:
            parsed = wrapper.run_svalint(
                fake.target('stderr'), svalint_root=fake.root)
        self.assertEqual(parsed.error_count, 1)
        self.assertEqual(parsed.violations[0].rule_id, 'RULE-ERR')

    def test_the_target_path_is_absolute_even_with_spaces(self):
        with FakeSVALint() as fake:
            target = fake.target('clean')
            wrapper.run_svalint(target, svalint_root=fake.root)
            args = fake.args_file.read_text(encoding='utf-8').splitlines()
        self.assertEqual(args[0], '-t')
        self.assertEqual(args[1], str(target.resolve()))
        self.assertTrue(Path(args[1]).is_absolute())

    def test_the_config_path_is_absolute_and_forwarded(self):
        with FakeSVALint() as fake:
            config = Path(fake.temp.name) / 'lint config.toml'
            config.write_text('rules = []', encoding='utf-8')
            wrapper.run_svalint(
                fake.target('clean'),
                svalint_root=fake.root,
                config_file=config,
            )
            captured = fake.config_capture.read_text(encoding='utf-8')
        self.assertEqual(captured, str(config.resolve()))

    def test_no_explicit_config_does_not_leak_a_stale_parent_value(self):
        with FakeSVALint() as fake:
            os.environ['SVALINT_CONFIG'] = '/stale/config'
            wrapper.run_svalint(
                fake.target('clean'), svalint_root=fake.root)
            captured = fake.config_capture.read_text(encoding='utf-8')
        self.assertEqual(captured, '<unset>')

    def test_a_missing_target_fails_closed_without_launching(self):
        with FakeSVALint() as fake:
            parsed = wrapper.run_svalint(
                Path(fake.temp.name) / 'missing.sv',
                svalint_root=fake.root,
            )
        self.assertFalse(parsed.passed)
        self.assertIn('property file', parsed.execution_error)
        self.assertNotEqual(parsed.returncode, 0)

    def test_a_missing_checkout_fails_closed_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'prop.sv'
            target.write_text('clean', encoding='utf-8')
            parsed = wrapper.run_svalint(
                target, svalint_root=Path(directory) / 'absent')
        self.assertFalse(parsed.passed)
        self.assertIn('root', parsed.execution_error.lower())

    def test_a_missing_entry_point_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'prop.sv'
            target.write_text('clean', encoding='utf-8')
            parsed = wrapper.run_svalint(target, svalint_root=Path(directory))
        self.assertFalse(parsed.passed)
        self.assertIn('bin/svalint.py', parsed.execution_error)

    def test_a_tool_crash_is_not_mistaken_for_zero_lint_errors(self):
        with FakeSVALint() as fake:
            parsed = wrapper.run_svalint(
                fake.target('crash'), svalint_root=fake.root)
        self.assertFalse(parsed.passed)
        self.assertEqual(parsed.returncode, 7)
        self.assertIn('status 7', parsed.execution_error)
        self.assertIn('internal parser traceback', parsed.stderr)

    def test_a_timeout_fails_closed_and_is_reported(self):
        with FakeSVALint() as fake:
            parsed = wrapper.run_svalint(
                fake.target('sleep'),
                svalint_root=fake.root,
                timeout_s=0.05,
            )
        self.assertFalse(parsed.passed)
        self.assertTrue(parsed.timed_out)
        self.assertIn('timed out', parsed.execution_error.lower())

    def test_an_os_error_is_returned_as_an_execution_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'prop.sv'
            target.write_text('clean', encoding='utf-8')
            root = Path(directory) / 'root'
            (root / 'bin').mkdir(parents=True)
            (root / 'bin' / 'svalint.py').write_text('', encoding='utf-8')
            with mock.patch(
                    'svalint_wrapper.subprocess.run',
                    side_effect=OSError('cannot execute')):
                parsed = wrapper.run_svalint(target, svalint_root=root)
        self.assertFalse(parsed.passed)
        self.assertIn('cannot execute', parsed.execution_error)


@unittest.skipUnless(
    REAL_SVALINT_READY,
    'installed SVALint requires anytree, tomli and verible-verilog-syntax',
)
class TestInstalledSVALintIntegration(unittest.TestCase):
    """Cross-check the wrapper against this container's actual checkout."""

    def test_local_checkout_is_the_default_when_environment_is_unset(self):
        with mock.patch.dict(os.environ):
            os.environ.pop('SVALINT_ROOT', None)
            parsed = wrapper.run_svalint(
                INSTALLED_SVALINT / 'tests' / 'test_10_p.sv')
        self.assertTrue(parsed.passed)
        self.assertEqual(parsed.execution_error, '')

    def test_stale_env_and_stripped_path_still_lint_from_tmp(self):
        old = os.getcwd()
        os.chdir('/tmp')
        try:
            with mock.patch.dict(os.environ, {
                    'SVALINT_ROOT': '/tmp/missing-SVALint',
                    'PATH': '/usr/bin:/bin'}):
                parsed = wrapper.run_svalint(
                    INSTALLED_SVALINT / 'tests' / 'test_10_p.sv')
        finally:
            os.chdir(old)
        self.assertEqual(parsed.execution_error, '')
        self.assertTrue(parsed.passed)

    def test_every_compile_design_prop_executes_from_tmp(self):
        designs = (
            'cic_decimator', 'ttc_counter_lite', 'alu_core', 'counter',
            'csr_apb_interface', 'generic_fifo_sc_a', 'vcnt',
            'present_encryptor_top', 'tlb', 'ptw', 'fpnew_top',
        )
        overlay = (
            Path(__file__).resolve().parents[1]
            / 'date_reduced_artifacts' / 'elaboration_preflight' / 'overlay'
        )
        missing = [
            design for design in designs
            if not (overlay / f'ft_{design}' / 'sva' / f'{design}_prop.sv').is_file()
        ]
        if missing:
            self.skipTest('elaboration preflight overlay is not staged: ' + ','.join(missing))
        old = os.getcwd()
        os.chdir('/tmp')
        try:
            with mock.patch.dict(os.environ, {
                    'SVALINT_ROOT': '/tmp/missing-SVALint',
                    'PATH': '/usr/bin:/bin'}):
                for design in designs:
                    parsed = wrapper.run_svalint(
                        overlay / f'ft_{design}' / 'sva' / f'{design}_prop.sv')
                    self.assertEqual(parsed.execution_error, '', design)
        finally:
            os.chdir(old)

    def test_a_real_known_clean_fixture_opens_the_gate(self):
        parsed = wrapper.run_svalint(
            INSTALLED_SVALINT / 'tests' / 'test_10_p.sv',
            svalint_root=INSTALLED_SVALINT,
        )
        self.assertTrue(parsed.passed)
        self.assertEqual((parsed.error_count, parsed.warning_count), (0, 0))
        self.assertEqual(parsed.violations, [])

    def test_real_violations_close_the_gate_despite_zero_exit_status(self):
        parsed = wrapper.run_svalint(
            INSTALLED_SVALINT / 'tests' / 'test_sva_missing_label_f.sv',
            svalint_root=INSTALLED_SVALINT,
        )
        self.assertEqual(parsed.returncode, 0)
        self.assertFalse(parsed.passed)
        self.assertEqual(parsed.error_count, 2)
        self.assertEqual(
            [item.rule_id for item in parsed.violations],
            ['ASSERT_MISSING_LABEL', 'FUNC_MISSING_FAIL_ABLK'],
        )
        self.assertIn('assert property', parsed.violations[0].assertion_text)
        self.assertEqual(parsed.stdout, '')
        self.assertIn('AsFigo: Violation:', parsed.stderr)

    def test_explicit_config_reaches_the_real_rule_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'selected-rules.toml'
            config.write_text(
                '[rules]\n'
                'ASSERT_MISSING_LABEL = false\n'
                'FUNC_MISSING_FAIL_ABLK = false\n',
                encoding='utf-8',
            )
            parsed = wrapper.run_svalint(
                INSTALLED_SVALINT / 'tests' / 'test_sva_missing_label_f.sv',
                svalint_root=INSTALLED_SVALINT,
                config_file=config,
            )
        self.assertTrue(parsed.passed)
        self.assertEqual(parsed.violations, [])


class TestOutputCombination(unittest.TestCase):
    def test_streams_receive_a_boundary_newline(self):
        self.assertEqual(
            wrapper._combine_output('ERROR : 0', 'WARNING : 0'),
            'ERROR : 0\nWARNING : 0',
        )

    def test_an_existing_boundary_newline_is_not_duplicated(self):
        self.assertEqual(
            wrapper._combine_output('ERROR : 0\n', 'WARNING : 0'),
            'ERROR : 0\nWARNING : 0',
        )

    def test_an_empty_stream_adds_no_noise(self):
        self.assertEqual(wrapper._combine_output('clean\n', ''), 'clean\n')
        self.assertEqual(wrapper._combine_output('', 'failure\n'), 'failure\n')


class TestSVALintOutputParsing(unittest.TestCase):
    def test_clean_summary_passes(self):
        parsed = parse(CLEAN)
        self.assertTrue(parsed.passed)
        self.assertEqual((parsed.error_count, parsed.warning_count), (0, 0))

    def test_standard_violation_is_parsed(self):
        parsed = parse(ONE_VIOLATION, returncode=1)
        self.assertFalse(parsed.passed)
        self.assertEqual(parsed.error_count, 1)
        self.assertEqual(len(parsed.violations), 1)
        self.assertEqual(parsed.violations[0].rule_id, 'ASFI-05')

    def test_two_violations_keep_order(self):
        parsed = parse(TWO_VIOLATIONS, returncode=1)
        self.assertEqual(
            [item.rule_id for item in parsed.violations],
            ['ASFI-05', 'ASFI-12'],
        )
        self.assertEqual((parsed.error_count, parsed.warning_count), (2, 1))

    def test_summary_spacing_and_case_are_flexible(self):
        parsed = parse('error:3\nwarning    :    4\n', returncode=1)
        self.assertEqual((parsed.error_count, parsed.warning_count), (3, 4))

    def test_crlf_output_is_normalised(self):
        parsed = parse(ONE_VIOLATION.replace('\n', '\r\n'), returncode=1)
        self.assertEqual(len(parsed.violations), 1)
        self.assertEqual(parsed.violations[0].rule_id, 'ASFI-05')

    def test_ansi_colour_does_not_hide_counts_or_violations(self):
        output = (
            '\x1b[31mAsFigo: Violation: [RULE-COLOR]:\x1b[0m\n'
            'bad\n'
            'a_bad: assert property ((bad_i))\n'
            '\x1b[31mERROR : 1\x1b[0m\nWARNING : 0\n'
        )
        parsed = parse(output, returncode=1)
        self.assertEqual(parsed.error_count, 1)
        self.assertEqual(parsed.violations[0].rule_id, 'RULE-COLOR')

    def test_the_last_summary_wins_over_progress_counters(self):
        parsed = parse(
            'ERROR : 4\nWARNING : 2\nwork...\nERROR : 0\nWARNING : 1\n')
        self.assertEqual((parsed.error_count, parsed.warning_count), (0, 1))
        self.assertTrue(parsed.passed)

    def test_a_violation_without_a_summary_still_counts_as_an_error(self):
        parsed = parse(
            'AsFigo: Violation: [RULE-X]:\nBad\n'
            'a_bad: assert property ((bad_i))\n',
            returncode=1,
        )
        self.assertEqual(parsed.error_count, 1)
        self.assertFalse(parsed.passed)

    def test_more_parsed_violations_than_the_summary_cannot_pass(self):
        parsed = parse(TWO_VIOLATIONS.replace('ERROR : 2', 'ERROR : 0'))
        self.assertEqual(parsed.error_count, 2)
        self.assertFalse(parsed.passed)

    def test_duplicate_violations_across_streams_are_deduplicated(self):
        parsed = parse(
            '',
            returncode=1,
            stdout=ONE_VIOLATION,
            stderr=ONE_VIOLATION,
        )
        self.assertEqual(len(parsed.violations), 1)
        self.assertEqual(parsed.error_count, 1)

    def test_rule_ids_are_trimmed(self):
        parsed = parse(
            'AsFigo: Violation: [  RULE-SPACE  ]:\nBad\n'
            'a_bad: assert property ((bad_i))\nERROR : 1\n',
            returncode=1,
        )
        self.assertEqual(parsed.violations[0].rule_id, 'RULE-SPACE')

    def test_a_warning_only_run_passes(self):
        parsed = parse('ERROR : 0\nWARNING : 7\n')
        self.assertTrue(parsed.passed)
        self.assertEqual(parsed.warning_count, 7)

    def test_nonzero_exit_without_lint_evidence_is_execution_failure(self):
        parsed = parse('traceback only\n', returncode=9)
        self.assertFalse(parsed.passed)
        self.assertEqual(parsed.error_count, 0)
        self.assertIn('status 9', parsed.execution_error)

    def test_nonzero_exit_with_lint_errors_is_not_mislabelled_as_a_crash(self):
        parsed = parse(ONE_VIOLATION, returncode=1)
        self.assertEqual(parsed.execution_error, '')

    def test_zero_exit_without_a_summary_is_allowed_when_output_is_empty(self):
        parsed = parse('')
        self.assertTrue(parsed.passed)


class TestAssertionExtraction(unittest.TestCase):
    def test_a_labelled_assertion_line_is_extracted(self):
        message = (
            'Violation detail\n'
            'a_req: assert property ((req_i |=> gnt_o))\n'
            'line 22\n'
        )
        self.assertEqual(
            wrapper._extract_assertion_from_message(message),
            'a_req: assert property ((req_i |=> gnt_o))',
        )

    def test_keyword_case_is_irrelevant(self):
        self.assertEqual(
            wrapper._extract_assertion_from_message(
                'A_REQ: ASSERT PROPERTY ((req_i))'),
            'A_REQ: ASSERT PROPERTY ((req_i))',
        )

    def test_later_prose_that_mentions_assert_property_does_not_win(self):
        message = (
            'a_req: assert property ((req_i))\n'
            'Use assert property with a failure action.\n'
        )
        self.assertEqual(
            wrapper._extract_assertion_from_message(message),
            'a_req: assert property ((req_i))',
        )

    def test_an_unlabelled_assertion_is_a_fallback(self):
        self.assertEqual(
            wrapper._extract_assertion_from_message(
                'detail\nassert property ((req_i));\n'),
            'assert property ((req_i));',
        )

    def test_no_assertion_returns_empty(self):
        self.assertEqual(
            wrapper._extract_assertion_from_message('ordinary prose'), '')


class TestLLMFeedbackFormatting(unittest.TestCase):
    def test_clean_result_needs_no_correction_prompt(self):
        self.assertEqual(wrapper.format_violations_for_llm(result()), '')

    def test_violations_include_counts_rules_messages_and_assertions(self):
        parsed = parse(TWO_VIOLATIONS, returncode=1)
        feedback = wrapper.format_violations_for_llm(parsed)
        self.assertIn('2 error(s) and 1 warning(s)', feedback)
        self.assertIn('1. [ASFI-05]', feedback)
        self.assertIn('2. [ASFI-12]', feedback)
        self.assertIn('a_clock: assert property', feedback)

    def test_unparsed_errors_include_both_output_streams(self):
        parsed = result(
            returncode=1,
            errors=2,
            stdout='stdout detail',
            stderr='stderr detail',
        )
        feedback = wrapper.format_violations_for_llm(parsed)
        self.assertIn('no detailed violations', feedback)
        self.assertIn('stdout detail', feedback)
        self.assertIn('stderr detail', feedback)

    def test_execution_failure_is_explicit_and_includes_diagnostics(self):
        parsed = result(
            returncode=7,
            execution_error='SVALint exited with status 7',
            stderr='traceback detail',
            passed=False,
        )
        feedback = wrapper.format_violations_for_llm(parsed)
        self.assertIn('could not complete', feedback)
        self.assertIn('status 7', feedback)
        self.assertIn('traceback detail', feedback)

    def test_timeout_feedback_does_not_ask_the_llm_to_rewrite_sva(self):
        parsed = result(
            returncode=124,
            execution_error='SVALint timed out after 10 seconds',
            passed=False,
        )
        feedback = wrapper.format_violations_for_llm(parsed)
        self.assertIn('could not complete', feedback)
        self.assertNotIn('Fix ALL', feedback)


class TestHumanSummaryFormatting(unittest.TestCase):
    def test_clean_summary(self):
        self.assertEqual(
            wrapper.format_violation_summary(result()),
            'SVALint: no errors',
        )

    def test_warning_only_summary_is_not_hidden(self):
        self.assertEqual(
            wrapper.format_violation_summary(result(warnings=3)),
            'SVALint: no errors, 3 warning(s)',
        )

    def test_lint_failure_summary(self):
        self.assertEqual(
            wrapper.format_violation_summary(
                result(returncode=1, errors=2, warnings=1, passed=False)),
            'SVALint: 2 error(s), 1 warning(s)',
        )

    def test_execution_failure_summary(self):
        self.assertEqual(
            wrapper.format_violation_summary(result(
                returncode=7,
                passed=False,
                execution_error='SVALint exited with status 7',
            )),
            'SVALint: execution failed — SVALint exited with status 7',
        )


class TestAgentLintGate(unittest.TestCase):
    @staticmethod
    def make_agent():
        instance = agent_module.CodingAgent.__new__(agent_module.CodingAgent)
        instance.assertions_dir = '/tmp/'
        instance.assertions_file = 'properties.sv'
        instance._log = lambda *_args, **_kwargs: None
        return instance

    def test_clean_result_opens_the_gate(self):
        instance = self.make_agent()
        parsed = result()
        with mock.patch('agent.run_svalint', return_value=parsed):
            self.assertTrue(instance._run_svalint_check())
        self.assertIs(instance.current_lint_result, parsed)
        self.assertEqual(instance.current_error, '')

    def test_lint_errors_keep_the_gate_closed_and_supply_feedback(self):
        instance = self.make_agent()
        parsed = parse(ONE_VIOLATION, returncode=1)
        with mock.patch('agent.run_svalint', return_value=parsed):
            self.assertFalse(instance._run_svalint_check())
        self.assertIn('ASFI-05', instance.current_error)

    def test_tool_crash_keeps_the_gate_closed_even_with_zero_parsed_errors(self):
        instance = self.make_agent()
        parsed = result(
            returncode=7,
            errors=0,
            passed=False,
            execution_error='SVALint exited with status 7',
        )
        with mock.patch('agent.run_svalint', return_value=parsed):
            self.assertFalse(instance._run_svalint_check())
        self.assertIn('could not complete', instance.current_error)

    def test_warning_only_result_opens_the_gate(self):
        instance = self.make_agent()
        parsed = result(warnings=2)
        with mock.patch('agent.run_svalint', return_value=parsed):
            self.assertTrue(instance._run_svalint_check())

    def test_execution_failure_never_spends_an_llm_syntax_iteration(self):
        instance = self.make_agent()
        instance.syntax_iterations = 0
        instance.assertions = ['a_x: assert property ((x_i));']
        instance.formal_tool = 'vcformal'
        instance.metrics = {}
        instance._write_assertions = mock.Mock()
        instance._correct_assertions = mock.Mock()
        instance._get_syntax_errors = mock.Mock(return_value=True)
        instance._quarantine_defective_assertions = mock.Mock()
        instance._update_metrics = mock.Mock()
        instance._display_error_tracking_stats = mock.Mock()
        instance.current_lint_result = result(
            returncode=124,
            passed=False,
            execution_error='SVALint timed out after 10 seconds',
        )
        instance._run_svalint_check = mock.Mock(return_value=False)

        instance._correct_syntax()

        instance._correct_assertions.assert_not_called()
        self.assertEqual(instance.syntax_iterations, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
