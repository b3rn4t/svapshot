#!/usr/bin/env python3
"""Timeout finalization writes the same property files a finished run would."""

from __future__ import annotations

import os
import tempfile
import unittest

import _path_setup  # noqa: F401

from publication_eval.config import DesignConfig, ExperimentConfig
from publication_eval.runner import Cell
from publication_eval.timeout_snapshot import (
    finalize_cell_snapshot,
    write_full_property,
    write_only_proven,
)


HEADER = """\
module demo_sva (
  input logic clk
);
//====DESIGNER-ADDED-SVA====//
"""

PROVEN = """\
a_ok: assert property (@(posedge clk) 1);
"""

FAILING = """\
a_bad: assert property (@(posedge clk) 0);
"""


class TestTimeoutSnapshot(unittest.TestCase):

    def test_restore_after_extension_not_the_one_assert_repair_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = os.path.join(tmp, 'ft_demo')
            sva = os.path.join(harness, 'sva')
            versions = os.path.join(sva, 'versions')
            os.makedirs(versions)
            write_full_property(
                os.path.join(versions, 'demo_prop_after_extension.sv'),
                HEADER, [PROVEN, FAILING])
            write_full_property(
                os.path.join(sva, 'demo_prop.sv'), HEADER, [FAILING])
            reports = os.path.join(tmp, 'artifacts', 'generation_and_extension',
                                   'vcf_projs', 'demo', 'reports')
            os.makedirs(reports)
            with open(os.path.join(reports, 'properties.rpt'), 'w') as handle:
                handle.write("""
  Summary Results
   Property Summary: FPV
   > Assertion
     - # found        : 2
     - # proven       : 1
     - # falsified    : 1
  Verbose Results
   > Assertion
     > ID: [0] proven
      - name          : demo.u_demo_sva.a_ok
     > ID: [1] falsified (depth=3)
      - name          : demo.u_demo_sva.a_bad
""")
            cell_dir = tmp
            os.makedirs(os.path.join(cell_dir, 'runs'), exist_ok=True)
            experiment = ExperimentConfig(
                path=os.path.join(tmp, 'exp.yaml'),
                root=tmp,
                raw={'artifact_root': tmp, 'study_id': 't'},
                designs=[DesignConfig(
                    id='demo', module='demo', suite='', stratum='low',
                    rtl='demo.sv')],
            )
            cell = Cell('controlled', 'demo', 'svapshot', 'cursor:grok-4.6', 1729, 1)
            result = finalize_cell_snapshot(experiment, cell, cell_dir)
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['assertion_count'], 2)
            live = open(os.path.join(sva, 'demo_prop.sv'), encoding='utf-8').read()
            self.assertIn('a_ok:', live)
            self.assertIn('a_bad:', live)
            after = open(
                os.path.join(versions, 'demo_prop_after_semantic_correction.sv'),
                encoding='utf-8',
            ).read()
            self.assertIn('a_ok:', after)
            self.assertIn('a_bad:', after)
            proven = open(
                os.path.join(sva, 'demo_prop_only_proven.sv'), encoding='utf-8',
            ).read()
            self.assertIn('a_ok:', proven)
            self.assertIn('// a_bad:', proven)
            self.assertTrue(os.path.isfile(os.path.join(cell_dir, 'snapshot_metrics.json')))

    def test_only_proven_comments_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'only.sv')
            proved, total = write_only_proven(
                path, HEADER, [PROVEN, FAILING],
                {'a_ok': 'proven', 'a_bad': 'cex'})
            self.assertEqual((proved, total), (1, 2))
            text = open(path, encoding='utf-8').read()
            self.assertRegex(text, r'(?m)^a_ok:')
            self.assertIn('// a_bad:', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
