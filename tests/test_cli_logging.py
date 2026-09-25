#!/usr/bin/env python3
"""Orchestrator CLI logging: quiet STEP progress and one-line package hits."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
CORE = os.path.join(REPO, 'src', 'core')
for path in (REPO, CORE):
    if path not in sys.path:
        sys.path.insert(0, path)

import cli_log
import main


class TestSummarizePackageHits(unittest.TestCase):
    def test_one_line_per_package_even_with_many_hits(self):
        hits = {
            'fpnew_pkg': [('scope', 17), ('scope', 17), ('pattern', 17),
                          ('type', 17), ('scope', 21), ('scope', 22)],
            'other_pkg': [('import', 3)],
        }
        lines = cli_log.summarize_package_hits(hits)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith('📦 Package fpnew_pkg:'))
        self.assertIn('6 references', lines[0])
        self.assertIn('first scope at line 17', lines[0])
        self.assertEqual(lines[1], '📦 Package other_pkg: import (line 3)')


class TestDetectPackagesOneLinePerPackage(unittest.TestCase):
    def test_repeated_scope_hits_collapse_to_one_summary_line(self):
        root = tempfile.mkdtemp(prefix='pkg_log_')
        rtl = os.path.join(root, 'dut.sv')
        with open(rtl, 'w', encoding='utf-8') as handle:
            handle.write(
                'module dut;\n'
                '  fpnew_pkg::roundmode_e a;\n'
                '  fpnew_pkg::fp_format_e b;\n'
                '  fpnew_pkg::operation_e c;\n'
                'endmodule\n'
            )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main.detect_packages_from_rtl(rtl, [root], root)
        text = buf.getvalue()
        self.assertEqual(text.count('📦 Package fpnew_pkg:'), 1)
        self.assertNotIn('Found package scope usage', text)
        self.assertNotIn('Found package pattern usage', text)
        self.assertNotIn('Found package type pattern', text)


class TestFlowCliQuietSteps(unittest.TestCase):
    def tearDown(self):
        cli_log.reset('medium')

    def test_low_buffers_detail_and_keeps_success_trail_off_stdout(self):
        out = io.StringIO()
        err = io.StringIO()
        cli = cli_log.configure('low', stream=out, progress_stream=err, spinner_interval=0.02)
        with cli.step('🔧 STEP 1: example') as step:
            print('fine grain detail that must not leak')
            time.sleep(0.05)
        self.assertNotIn('fine grain detail', out.getvalue())
        self.assertIn('✅ 🔧 STEP 1: example', err.getvalue())
        self.assertTrue(step.ok)

    def test_low_dumps_the_buffered_trail_when_a_step_fails(self):
        out = io.StringIO()
        err = io.StringIO()
        # Capture sys.__stdout__ used by FlowCli on failure dump.
        real_stdout = sys.__stdout__
        dump = io.StringIO()
        cli = cli_log.configure('low', stream=out, progress_stream=err, spinner_interval=0.02)
        with mock.patch.object(sys, '__stdout__', dump):
            with cli.step('🔧 STEP 1: example') as step:
                print('hidden until failure')
                print('❌ boom')
                step.fail()
        self.assertIn('hidden until failure', dump.getvalue())
        self.assertIn('❌ boom', dump.getvalue())
        self.assertNotIn('✅', err.getvalue())
        # Avoid leaving a closed mock as __stdout__ if patch somehow missed restore.
        self.assertIs(sys.__stdout__, real_stdout)

    def test_medium_still_prints_the_full_banner_and_detail(self):
        out = io.StringIO()
        err = io.StringIO()
        cli = cli_log.configure('medium', stream=out, progress_stream=err)
        with contextlib.redirect_stdout(out):
            with cli.step('🔧 STEP 1: example'):
                print('visible detail')
        text = out.getvalue()
        self.assertIn('=' * 60, text)
        self.assertIn('🔧 STEP 1: example', text)
        self.assertIn('visible detail', text)
        self.assertEqual(err.getvalue(), '')

    def test_spinner_alternates_prefixes_while_the_step_runs(self):
        err = io.StringIO()
        cli = cli_log.configure('low', stream=io.StringIO(), progress_stream=err, spinner_interval=0.01)
        started = threading.Event()

        def work():
            started.set()
            time.sleep(0.08)

        with cli.step('🔧 STEP 1: spin'):
            work()
        progress = err.getvalue()
        self.assertTrue('.:' in progress or ':.' in progress)
        self.assertIn('STEP 1: spin', progress)


class TestVerbosityDefaultIsLow(unittest.TestCase):
    def test_omitting_verbosity_selects_low(self):
        args = main.parse_arguments(['alu.sv', 'a/model', 'golden'])
        self.assertEqual(args.verbosity, 'low')
        self.assertEqual(args.execution_type, 'golden')

    def test_an_explicit_medium_still_works(self):
        args = main.parse_arguments(['alu.sv', 'a/model', 'golden', 'medium'])
        self.assertEqual(args.verbosity, 'medium')

    def test_module_type_and_verbosity_still_compose(self):
        args = main.parse_arguments(
            ['alu.sv', 'a/model', 'sequential', 'golden', 'high'])
        self.assertEqual(args.module_type, 'sequential')
        self.assertEqual(args.verbosity, 'high')


if __name__ == '__main__':
    unittest.main(verbosity=2)
