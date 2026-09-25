#!/usr/bin/env python3
"""Classify DATE generation-log compile loops without a formal tool."""

from __future__ import annotations

import unittest

import _path_setup  # noqa: F401

import os
import tempfile
from datetime import datetime, timezone

from publication_eval.generation_health import (
    Finding,
    cell_log_text,
    inspect_cell,
    inspect_log,
    stop_needles,
    write_health,
)


FVEVAL_IND = """
Correcting assertion syntax with SVALint...
Running final VC Formal compile check...
Error-[IND] Identifier not declared
  Identifier 'width' has not been declared
Assertions contain syntax errors
FPV compile failed but no failing assertion could be identified
Stored LLM interaction for syntax stage iteration 0
Requesting LLM to correct assertion syntax...
Running final VC Formal compile check...
Error-[IND] Identifier not declared
  Identifier 'width' has not been declared
Assertions contain syntax errors
FPV compile failed but no failing assertion could be identified
Stored LLM interaction for syntax stage iteration 1
"""

HEALTHY_AFTER_FIX = """
Assertions contain syntax errors
FPV syntax error in assertion "a_inc_from_zero_needs_rate"
Stored LLM interaction for syntax stage iteration 0
Assertions compiled successfully
VC Formal compile check passed
[Info] PROP_I_RESULT: FPV  cic.u_sva.a_x  e1  proven  property
"""

LICENSE_ONLY = """
VC Formal could not check out a license
Unable to checkout required license from the license server
"""

SYNTAX_LOOP = """
Stored LLM interaction for syntax stage iteration 0
Assertions contain syntax errors
Stored LLM interaction for syntax stage iteration 3
Assertions contain syntax errors
Targeted FPV fix attempt 3/3 for a_foo
Targeted FPV fix attempt 3/3 for a_bar
Stored LLM interaction for syntax stage iteration 8
Assertions contain syntax errors
"""


class TestInspectLog(unittest.TestCase):

    def test_fveval_width_is_a_tool_compile_bug(self):
        finding = inspect_log(FVEVAL_IND)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.kind, 'tool_compile_bug')
        self.assertIn('width', finding.identifiers)
        self.assertGreaterEqual(finding.compile_fail_count, 2)

    def test_successful_compile_after_a_typo_is_healthy(self):
        self.assertIsNone(inspect_log(HEALTHY_AFTER_FIX))

    def test_license_miss_is_not_a_compile_loop(self):
        self.assertIsNone(inspect_log(LICENSE_ONLY))

    def test_exhausted_syntax_iterations_are_a_loop(self):
        finding = inspect_log(SYNTAX_LOOP)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.kind, 'syntax_loop')
        self.assertGreaterEqual(finding.syntax_iterations, 8)

    def test_stale_overlay_vcf_log_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = os.path.join(tmp, 'fveval_counter__one_shot__s1729')
            os.makedirs(overlay)
            leftover = os.path.join(overlay, 'vcf.log')
            with open(leftover, 'w', encoding='utf-8') as handle:
                handle.write(FVEVAL_IND)
            old = datetime(2026, 9, 18, tzinfo=timezone.utc).timestamp()
            os.utime(leftover, (old, old))
            cell = os.path.join(tmp, 'cell')
            os.makedirs(cell)
            manifest = {
                'created_utc': '2026-09-20T00:09:37+00:00',
                'started_utc': '2026-09-20T00:09:37+00:00',
                'environment': {'cell_workspace': overlay},
                'cell': {
                    'design_id': 'fveval_counter',
                    'method': 'one_shot',
                    'seed': 1729,
                },
            }
            self.assertEqual(cell_log_text(
                cell, [leftover], created_epoch=datetime(
                    2026, 9, 20, tzinfo=timezone.utc).timestamp()), '')
            self.assertIsNone(inspect_cell(cell, manifest))

    def test_health_file_names_the_required_fix(self):
        finding = Finding(
            kind='tool_compile_bug',
            reason='Error-[IND]',
            identifiers=['width'],
        )
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = write_health(
                tmp, 'cell-s', 'fveval_counter', finding,
                stopped=True, method='svapshot')
            self.assertEqual(snapshot['required_action'], 'svapshot_bugfix')
            oneshot = write_health(
                tmp, 'cell-o', 'cic_decimator', finding,
                stopped=True, method='one_shot')
            self.assertEqual(oneshot['required_action'], 'oneshot_manual_fix')

    def test_one_shot_ind_does_not_stop_the_whole_design(self):
        needles = stop_needles(
            cell_id='controlled-fveval-one_shot-r1',
            design_id='fveval_counter',
            overlay='/tmp/fveval_counter__one_shot__s1729',
            kind='tool_compile_bug',
        )
        self.assertNotIn('--design fveval_counter', needles)


if __name__ == '__main__':
    unittest.main(verbosity=2)
