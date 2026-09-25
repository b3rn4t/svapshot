#!/usr/bin/env python3
"""In-place formal substitutions must recover and never race."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import _path_setup  # noqa: F401

from formal_harness import FormalHarness


class TestSafeSubstitution(unittest.TestCase):

    def setUp(self):
        self.harness = object.__new__(FormalHarness)

    def test_normal_context_restores_original_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'dut.sv')
            path.write_text('original\n', encoding='utf-8')
            with self.harness.substituted(str(path), 'mutant\n'):
                self.assertEqual(path.read_text(encoding='utf-8'), 'mutant\n')
            self.assertEqual(path.read_text(encoding='utf-8'), 'original\n')
            self.assertFalse(Path(str(path) + '.svapshot.lock').exists())
            self.assertFalse(Path(str(path) + '.svapshot.backup').exists())

    def test_dead_writer_is_recovered_before_next_substitution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'dut.sv')
            path.write_text('stranded-mutant\n', encoding='utf-8')
            Path(str(path) + '.svapshot.backup').write_text(
                'original\n', encoding='utf-8')
            Path(str(path) + '.svapshot.lock').write_text(
                '999999999', encoding='utf-8')

            with self.harness.substituted(str(path), 'next-mutant\n'):
                self.assertEqual(
                    path.read_text(encoding='utf-8'), 'next-mutant\n')

            self.assertEqual(path.read_text(encoding='utf-8'), 'original\n')

    def test_live_writer_lock_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'dut.sv')
            path.write_text('original\n', encoding='utf-8')
            Path(str(path) + '.svapshot.backup').write_text(
                'original\n', encoding='utf-8')
            Path(str(path) + '.svapshot.lock').write_text(
                str(os.getpid()), encoding='utf-8')

            with self.assertRaisesRegex(RuntimeError, 'live pid'):
                with self.harness.substituted(str(path), 'mutant\n'):
                    pass


if __name__ == '__main__':
    unittest.main(verbosity=2)
