#!/usr/bin/env python3
"""Unit tests for reading a Verilog design.

SVApshot writes SystemVerilog assertions whatever the design under test is
written in: SVA is a SystemVerilog feature, the checker is a separate module
bound into the DUT, and ``bind`` into a Verilog module is legal.  What has to
change per language is the RTL *reader*, and the designs that matter are not
merely older Verilog but written in the older style: the OpenCores FIFOs list
bare port names in the header, declare their directions in the body, put several
modules in one file, and use ``do`` — a SystemVerilog keyword — as a port name.

These tests pin what the reader must get right for such a design, and that the
answers for an ANSI header (Verilog-2001 or SystemVerilog) do not move.  No
formal tool, no LLM, no licence.

Run with ``python3 tests/test_verilog_reading.py``.
"""

from __future__ import annotations

import glob
import os
import sys
import unittest

import _path_setup  # noqa: F401, E402

import coi
import rtl_clocking

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINES = os.path.join(REPO, 'benchmarks', '2605.06434')
FIFO_DIR = os.path.join(BASELINES, 'fifo')
ALU_DIR = os.path.join(BASELINES, 'alu')


def read(path):
    with open(path, errors='replace') as handle:
        return handle.read()


#: A Verilog-95 header: bare names in the header, directions in the body, the
#: parameters in the body too, and one port declared twice (as an output and as
#: the register that drives it).
NON_ANSI = """\
module counter(clk, rst, up, count, carry);

parameter WIDTH = 8;

input            clk, rst;
input            up;
output [WIDTH-1:0] count;
output           carry;

reg [WIDTH-1:0]  count;
reg              carry;
wire             at_top;

assign at_top = (count == {WIDTH{1'b1}});

always @(posedge clk or negedge rst)
    if (!rst)      count <= 0;
    else if (up)   count <= count + 1'b1;

always @(posedge clk or negedge rst)
    if (!rst)      carry <= 1'b0;
    else           carry <= at_top & up;

endmodule
"""

#: The same interface in Verilog-2001 ANSI style, which the reader already read
#: correctly and must go on reading the same way.
ANSI = """\
module counter #(parameter WIDTH = 8) (
    input  wire              clk,
    input  wire              rst,
    input  wire              up,
    output reg  [WIDTH-1:0]  count,
    output reg               carry
);

wire at_top;
assign at_top = (count == {WIDTH{1'b1}});

always @(posedge clk or negedge rst)
    if (!rst)      count <= 0;
    else if (up)   count <= count + 1'b1;

endmodule
"""

#: Three modules in one file, which is ordinary in Verilog and which the reader
#: used to collapse into whichever came first.
THREE_MODULES = """\
module first(a, y);
input a;
output y;
wire scratch_first;
assign y = ~a;
endmodule

module second(b, z);
input b;
output z;
wire scratch_second;
assign z = b;
endmodule

module third(c, w);
input c;
output w;
wire scratch_third;
assign w = c;
endmodule
"""

#: Legal Verilog that SystemVerilog would reject: three of these names are
#: SystemVerilog keywords.
KEYWORD_CLASH = """\
module memory(clk, di, do, we);
input        clk, we;
input  [7:0] di;
output [7:0] do;
reg    [7:0] do;
reg          stable;
reg          logic_enable;
always @(posedge clk) if (we) do <= di;
endmodule
"""


class TestTheModuleAsked(unittest.TestCase):
    """A file with several modules describes several designs, not one."""

    def test_each_module_reports_its_own_ports(self):
        for name, port_in, port_out in (('first', 'a', 'y'),
                                        ('second', 'b', 'z'),
                                        ('third', 'c', 'w')):
            with self.subTest(module=name):
                graph = coi.build_signal_graph(THREE_MODULES, name, 'verilog')
                self.assertEqual(graph.ports_in, {port_in})
                self.assertEqual(graph.ports_out, {port_out})

    def test_a_module_does_not_inherit_the_next_ones_signals(self):
        graph = coi.build_signal_graph(THREE_MODULES, 'first', 'verilog')
        self.assertEqual(graph.internals, {'scratch_first'})

    def test_the_last_module_is_readable_too(self):
        # Bounding the body at endmodule must not stop at the *first* one.
        graph = coi.build_signal_graph(THREE_MODULES, 'third', 'verilog')
        self.assertEqual(graph.internals, {'scratch_third'})

    def test_a_name_the_file_does_not_declare_falls_back_to_the_first(self):
        # Callers pass labels as well as module names (a property module is
        # asked about under the DUT's name), and refusing those would lose the
        # only module the text has.
        graph = coi.build_signal_graph(THREE_MODULES, 'not_here', 'verilog')
        self.assertEqual(graph.ports_in, {'a'})

    def test_the_vendor_variants_of_the_ram_are_told_apart(self):
        if not os.path.isdir(FIFO_DIR):
            self.skipTest('the OpenCores baseline designs are not present')
        text = read(os.path.join(FIFO_DIR, 'generic_dpram.v'))
        generic = coi.build_signal_graph(text, 'generic_dpram', 'verilog')
        altera = coi.build_signal_graph(text, 'altera_ram_dp', 'verilog')
        xilinx = coi.build_signal_graph(text, 'xilinx_ram_dp', 'verilog')
        self.assertEqual(generic.ports_out, {'do'})
        self.assertEqual(altera.ports_out, {'q'})
        self.assertEqual(xilinx.ports_out, {'DOA', 'DOB'})


class TestDirectionsInAVerilog95Header(unittest.TestCase):
    """An output that passes for an input is worse than an unknown signal.

    The only rule governing what an environment assumption may constrain is
    'inputs only'.  An output the reader calls an input can therefore be
    assumed, and assuming an output turns the property being proven into a
    hypothesis, so this is a soundness question, not a cosmetic one.
    """

    def setUp(self):
        self.graph = coi.build_signal_graph(NON_ANSI, 'counter', 'verilog')

    def test_the_inputs_are_inputs(self):
        self.assertEqual(self.graph.ports_in, {'clk', 'rst', 'up'})

    def test_the_outputs_are_not_read_as_inputs(self):
        self.assertEqual(self.graph.ports_out, {'count', 'carry'})

    def test_a_port_that_is_also_a_register_stays_a_port(self):
        # 'output count;' and 'reg count;' declare one port, not a port and an
        # internal of the same name.
        self.assertNotIn('count', self.graph.internals)
        self.assertNotIn('carry', self.graph.internals)

    def test_the_internals_are_the_signals_that_are_not_ports(self):
        self.assertEqual(self.graph.internals, {'at_top'})

    def test_a_parameter_declared_in_the_body_is_found(self):
        self.assertIn('WIDTH', self.graph.parameters)

    def test_an_initialized_wire_is_still_a_signal(self):
        # OpenCores I2C writes `wire rst_n = arst_n ^ ARST_LVL;`. The `=` used
        # to make is_declaration reject the line, so derived resets vanished
        # from the graph and from every cone that needed them.
        text = NON_ANSI.replace(
            'wire             at_top;',
            'wire at_top;\n'
            'wire rst_n = rst ^ 1\'b0;\n'
            'wire wb_wacc = up & carry;\n')
        graph = coi.build_signal_graph(text, 'counter', 'verilog')
        self.assertIn('rst_n', graph.internals)
        self.assertIn('wb_wacc', graph.internals)
        self.assertIn('rst', graph.fan_in['rst_n'])
        self.assertIn('up', graph.fan_in['wb_wacc'])

    def test_fan_in_is_built_from_classic_always_blocks(self):
        self.assertIn('at_top', self.graph.fan_in['carry'])
        self.assertIn('up', self.graph.fan_in['carry'])

    def test_a_subprograms_arguments_are_not_ports(self):
        text = NON_ANSI.replace(
            'wire             at_top;',
            'wire at_top;\n'
            'function [7:0] twice;\n'
            '    input [7:0] value;\n'
            '    twice = value << 1;\n'
            'endfunction\n')
        graph = coi.build_signal_graph(text, 'counter', 'verilog')
        self.assertNotIn('value', graph.ports_in)
        self.assertEqual(graph.ports_in, {'clk', 'rst', 'up'})

    def test_an_ansi_header_reads_the_same_as_before(self):
        graph = coi.build_signal_graph(ANSI, 'counter', 'verilog')
        self.assertEqual(graph.ports_in, {'clk', 'rst', 'up'})
        self.assertEqual(graph.ports_out, {'count', 'carry'})

    def test_the_two_styles_describe_the_same_interface(self):
        ansi = coi.build_signal_graph(ANSI, 'counter', 'verilog')
        self.assertEqual(self.graph.ports_in, ansi.ports_in)
        self.assertEqual(self.graph.ports_out, ansi.ports_out)


class TestVerilogIdentifiersSystemVerilogReserves(unittest.TestCase):
    """``do`` is a keyword in SystemVerilog and a port name in Verilog."""

    def test_a_port_named_do_survives(self):
        graph = coi.build_signal_graph(KEYWORD_CLASH, 'memory', 'verilog')
        self.assertIn('do', graph.ports_out)

    def test_signals_named_after_later_keywords_survive(self):
        graph = coi.build_signal_graph(KEYWORD_CLASH, 'memory', 'verilog')
        self.assertIn('stable', graph.internals)

    def test_reading_it_as_systemverilog_loses_the_port(self):
        # Documents why the language has to be passed in rather than guessed:
        # the SystemVerilog keyword set deletes the design's data output.
        graph = coi.build_signal_graph(KEYWORD_CLASH, 'memory', 'sv')
        self.assertNotIn('do', graph.ports_out)

    def test_the_verilog_keywords_are_still_keywords(self):
        graph = coi.build_signal_graph(KEYWORD_CLASH, 'memory', 'verilog')
        for keyword in ('reg', 'wire', 'input', 'output', 'always', 'posedge'):
            with self.subTest(keyword=keyword):
                self.assertNotIn(keyword, graph.all_signals)

    def test_assertion_text_is_still_read_as_systemverilog(self):
        # The checker is SystemVerilog whatever the DUT is, so '$stable' must
        # not turn into a reference to a signal called 'stable'.
        self.assertNotIn('stable', coi.extract_identifiers('$stable(count_q)'))

    def test_the_language_follows_the_file_extension(self):
        self.assertEqual(coi.language_of('rtl/generic_fifo_sc_a.v'), 'verilog')
        self.assertEqual(coi.language_of('rtl/defines.vh'), 'verilog')
        self.assertEqual(coi.language_of('rtl/div_unit.sv'), 'sv')
        self.assertEqual(coi.language_of('rtl/pkg.svh'), 'sv')


@unittest.skipUnless(os.path.isdir(FIFO_DIR), 'the OpenCores baseline designs are not present')
class TestTheVerilogBenchmarks(unittest.TestCase):
    """The OpenCores FIFO family, which is what the benchmarks actually look like."""

    @classmethod
    def setUpClass(cls):
        cls.text = read(os.path.join(FIFO_DIR, 'generic_fifo_sc_a.v'))
        cls.graph = coi.build_signal_graph(cls.text, 'generic_fifo_sc_a', 'verilog')

    def test_the_status_outputs_are_outputs(self):
        for port in ('dout', 'full', 'empty', 'full_r', 'empty_r', 'level'):
            with self.subTest(port=port):
                self.assertIn(port, self.graph.ports_out)
                self.assertNotIn(port, self.graph.ports_in)

    def test_the_control_inputs_are_inputs(self):
        self.assertEqual(self.graph.ports_in,
                         {'clk', 'rst', 'clr', 'din', 'we', 're'})

    def test_the_pointers_are_internal(self):
        for signal in ('wp', 'rp', 'cnt', 'gb'):
            with self.subTest(signal=signal):
                self.assertIn(signal, self.graph.internals)

    def test_the_parameters_are_found(self):
        self.assertEqual(self.graph.parameters, {'dw', 'aw', 'n', 'max_size'})

    def test_no_module_in_the_family_reports_only_inputs(self):
        # Every one of these headers is non-ANSI: before directions were read
        # from the body, all seven modules reported an empty output set.
        for path in sorted(glob.glob(os.path.join(FIFO_DIR, '*.v'))):
            text = read(path)
            for name in ('generic_dpram', 'generic_fifo_dc', 'generic_fifo_dc_gray',
                         'generic_fifo_lfsr', 'generic_fifo_sc_a',
                         'generic_fifo_sc_b', 'lfsr'):
                if 'module ' + name not in text:
                    continue
                with self.subTest(module=name):
                    graph = coi.build_signal_graph(text, name, 'verilog')
                    self.assertTrue(graph.ports_out,
                                    '%s reports no outputs' % name)

    def test_the_clock_and_reset_are_recognised(self):
        # 'always @(posedge clk `SC_FIFO_ASYNC_RESET)' hides the reset edge
        # behind a macro, so the reset can only come from the port list.
        clocking = rtl_clocking.analyze_clocking(
            self.text, [p for p in ('clk', 'rst', 'clr', 'din', 'we', 're')
                        if p in self.graph.ports_in])
        self.assertTrue(clocking.is_sequential)
        self.assertEqual(clocking.clock, 'clk')
        self.assertEqual(clocking.reset, 'rst')
        self.assertTrue(clocking.reset_active_low)

    def test_an_internal_derived_reset_yields_to_its_driving_port(self):
        # AssertLLM I2C: always @(posedge wb_clk_i or negedge rst_n) with
        # `wire rst_n = arst_n ^ ARST_LVL`. The checker cannot declare rst_n.
        text = """\
module i2c_like(wb_clk_i, wb_rst_n, arst_n, q);
input wb_clk_i, wb_rst_n, arst_n;
output q;
reg q;
parameter ARST_LVL = 1'b0;
wire rst_n = arst_n ^ ARST_LVL;
always @(posedge wb_clk_i or negedge rst_n)
  if (!rst_n) q <= 1'b0;
  else if (wb_rst_n) q <= 1'b0;
  else q <= 1'b1;
endmodule
"""
        ports = ['wb_clk_i', 'wb_rst_n', 'arst_n']
        clocking = rtl_clocking.analyze_clocking(text, ports)
        self.assertEqual(clocking.clock, 'wb_clk_i')
        self.assertEqual(clocking.reset, 'arst_n')
        self.assertTrue(clocking.reset_active_low)
        self.assertNotEqual(clocking.reset, 'rst_n')

    def test_an_ansi_verilog_design_reads_the_same_way(self):
        if not os.path.isfile(os.path.join(ALU_DIR, 'alu_seq.v')):
            self.skipTest('the OpenCores baseline designs are not present')
        graph = coi.build_signal_graph(
            read(os.path.join(ALU_DIR, 'alu_seq.v')), 'alu_seq', 'verilog')
        self.assertTrue(graph.ports_in)
        self.assertTrue(graph.ports_out)
        self.assertFalse(graph.ports_in & graph.ports_out)


if __name__ == '__main__':
    unittest.main(verbosity=2)
