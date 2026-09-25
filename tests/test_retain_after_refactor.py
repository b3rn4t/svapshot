#!/usr/bin/env python3
"""retain_after_refactor keeps only properties whose names still exist."""

from __future__ import annotations

import unittest

import _path_setup  # noqa: F401, E402

from retain_after_refactor import kept_assertions, retain_after_refactor  # noqa: E402

BASELINE = """
module AMOALU(
  input  [7:0]  io_mask,
  input  [4:0]  io_cmd,
  input  [63:0] io_lhs,
  input  [63:0] io_rhs,
  output [63:0] io_out
);
  wire adder_out;
  assign io_out = adder_out;
endmodule
"""

REFACTORED = """
module AMOALU(
  input  [7:0]  io_mask,
  input  [4:0]  io_cmd,
  input  [63:0] io_lhs,
  input  [63:0] io_rhs,
  output [63:0] io_out
);
  wire logic_out;
  assign io_out = logic_out;
endmodule
"""

ADD_PROP = "a_add: assert property (io_cmd == 5'h8 |-> io_out == io_lhs + io_rhs);"
DEAD_PROP = "a_adder: assert property (adder_out == io_lhs + io_rhs);"
LOGIC_PROP = "a_logic: assert property (logic_out == (io_lhs & io_rhs));"


class RetainAfterRefactorTest(unittest.TestCase):
    def test_drops_deleted_internal(self):
        rows = retain_after_refactor(
            [ADD_PROP, DEAD_PROP], REFACTORED, module_name='AMOALU',
        )
        kept = {row.name: row.kept for row in rows}
        self.assertTrue(kept['a_add'])
        self.assertFalse(kept['a_adder'])
        self.assertIn('adder_out', rows[1].missing)

    def test_touched_keeps_only_diff_signals(self):
        rows = retain_after_refactor(
            [ADD_PROP, LOGIC_PROP],
            REFACTORED,
            module_name='AMOALU',
            baseline_rtl=BASELINE,
            mode='touched',
        )
        by_name = {row.name: row for row in rows}
        self.assertFalse(by_name['a_add'].kept)
        self.assertTrue(by_name['a_logic'].kept)
        self.assertEqual(
            kept_assertions(rows),
            [LOGIC_PROP],
        )


if __name__ == '__main__':
    unittest.main()
