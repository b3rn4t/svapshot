"""Fail-fast VC Formal session helpers."""

import os
import tempfile
import time
import unittest

import _path_setup  # noqa: F401
from vcf_session import (
    prepare_vcf_session,
    release_vcf_session,
    session_dir,
    wait_for_file,
)


class TestVcfSession(unittest.TestCase):

    def test_wait_for_file_times_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, 'vcf.log')
            started = time.time()
            self.assertFalse(wait_for_file(missing, 0.2, poll_s=0.05))
            self.assertLess(time.time() - started, 2.0)

    def test_wait_for_file_sees_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'vcf.log')
            with open(path, 'w') as handle:
                handle.write('ok\n')
            self.assertTrue(wait_for_file(path, 1.0, poll_s=0.05))

    def test_release_removes_stale_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale = session_dir(tmp, 'demo')
            os.makedirs(stale)
            lock = os.path.join(stale, 'session.lock')
            with open(lock, 'w') as handle:
                handle.write('dead\n')
            release_vcf_session(tmp, 'demo')
            self.assertFalse(os.path.exists(lock))

    def test_prepare_is_a_no_op_when_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            prepare_vcf_session(tmp, 'demo')


class TestStopAfterScaffoldFlag(unittest.TestCase):

    def test_parse_arguments_accepts_stop_after_scaffold(self):
        import main as ai_sva_main
        args = ai_sva_main.parse_arguments([
            'mod.sv', 'cursor:grok-4.6', 'golden', 'medium',
            '--formal-tool', 'vcformal', '--stop-after-scaffold',
        ])
        self.assertTrue(args.stop_after_scaffold)
        with self.assertRaises(SystemExit):
            ai_sva_main.parse_arguments([
                'mod.sv', 'cursor:grok-4.6', 'golden', 'medium',
                '--stop-after-scaffold', '--stop-after-extension',
            ])
