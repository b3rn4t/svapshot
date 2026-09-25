#!/usr/bin/env python3
"""Unit tests for ``bind_internals`` port promotion and bind updates."""

from __future__ import annotations

import unittest

import _path_setup  # noqa: F401, E402

import bind_internals
import coi


RTL = """\
module tiny (
    input  logic clk_i,
    input  logic [7:0] data_i,
    output logic [7:0] data_o
);
    logic [7:0] data_q;
    logic       helper;
    if (1) begin : gen_arbiter
        logic [3:0] rr_q;
        assign rr_q = data_i[3:0];
    end
    always_ff @(posedge clk_i) data_q <= data_i;
    assign data_o = data_q;
endmodule
"""

PROP = """\
module tiny_prop (
		input  logic       clk_i,
		input  logic [7:0] data_i,
		input  logic [7:0] data_o //output
	);

//====DESIGNER-ADDED-SVA====//
"""

BIND = """\
bind tiny tiny_prop
	#(
		.ASSERT_INPUTS (0)
	) u_tiny_sva(.*);
"""


class TestBindInternalsRewrite(unittest.TestCase):
    def setUp(self):
        self.graph = coi.build_signal_graph(RTL, 'tiny')

    def test_unqualified_internal_gets_port_and_keeps_dot_star(self):
        assertions = [
            'a_q: assert property ((data_q == data_i))\n'
            'else begin\n    $display("x");\nend',
        ]
        result, prop, bind = bind_internals.apply_port_promotion(
            assertions,
            module_name='tiny',
            graph=self.graph,
            rtl_text=RTL,
            prop_text=PROP,
            bind_text=BIND,
        )
        self.assertIn('data_q == data_i', result.assertions[0])
        self.assertNotIn('tiny.data_q', result.assertions[0])
        self.assertIn('data_q', result.needed_ports)
        self.assertIn('input logic [7:0] data_q', prop)
        self.assertIn('.*', bind)
        self.assertIsNone(result.needed_ports['data_q'].hierarchical_path)

    def test_module_prefix_is_stripped(self):
        assertions = [
            'a_q: assert property ((tiny.data_q == data_i))\n'
            'else begin\n    $display("x");\nend',
        ]
        result, prop, _ = bind_internals.apply_port_promotion(
            assertions,
            module_name='tiny',
            graph=self.graph,
            rtl_text=RTL,
            prop_text=PROP,
            bind_text=BIND,
        )
        self.assertIn('data_q == data_i', result.assertions[0])
        self.assertNotIn('tiny.data_q', result.assertions[0])
        self.assertIn('data_q', prop)

    def test_generate_scoped_signal_gets_explicit_bind(self):
        assertions = [
            'a_rr: assert property ((rr_q == data_i[3:0]))\n'
            'else begin\n    $display("x");\nend',
        ]
        result, prop, bind = bind_internals.apply_port_promotion(
            assertions,
            module_name='tiny',
            graph=self.graph,
            rtl_text=RTL,
            prop_text=PROP,
            bind_text=BIND,
        )
        self.assertIn('rr_q', result.needed_ports)
        self.assertIn('rr_q', prop)
        binding = result.needed_ports['rr_q']
        self.assertEqual(binding.hierarchical_path, 'gen_arbiter.rr_q')
        self.assertIn('.rr_q(gen_arbiter.rr_q)', bind)
        self.assertIn('.*', bind)

    def test_hierarchical_override_from_rfc_paths(self):
        overrides = bind_internals.hierarchical_overrides_from_rfc([
            'gen_arbiter.rr_q',
            'u_tiny_sva.helper',
        ])
        self.assertEqual(overrides['rr_q'], 'gen_arbiter.rr_q')
        self.assertEqual(overrides['helper'], 'u_tiny_sva.helper')

    def test_parse_rfc_obj_not_found(self):
        log = (
            'Warning-[RFC_OBJ_NOT_FOUND] Cannot find object gen_arbiter.rr_q\n'
            'Some other noise\n'
            'RFC_OBJ_NOT_FOUND: unknown signal top.gen_level.sel\n'
        )
        paths = bind_internals.parse_rfc_obj_not_found(log)
        self.assertIn('gen_arbiter.rr_q', paths)
        self.assertIn('top.gen_level.sel', paths)

    def test_local_enum_is_copied_into_checker(self):
        rtl = """\
module walker (
    input logic clk_i,
    output logic done_o
);
    typedef enum logic [1:0] {
        S_IDLE,
        S_WALK
    } ptw_state;
    ptw_state current_state;
    assign done_o = (current_state == S_WALK);
endmodule
"""
        graph = coi.build_signal_graph(rtl, 'walker')
        assertions = [
            'a_walk: assert property ((current_state == S_WALK))\n'
        ]
        _, prop, _ = bind_internals.apply_port_promotion(
            assertions,
            module_name='walker',
            graph=graph,
            rtl_text=rtl,
            prop_text=PROP.replace('tiny_prop', 'walker_prop'),
            bind_text=BIND,
        )
        self.assertIn('typedef enum logic [1:0]', prop)
        self.assertLess(prop.index('typedef enum'), prop.index('module walker_prop'))
        self.assertIn('input logic [1:0] current_state', prop)
        self.assertNotIn('input ptw_state current_state', prop)
        self.assertIn('S_WALK', prop)

    def test_packed_struct_array_not_confused_with_index_assign(self):
        rtl = """\
module walker (
    input logic clk_i
);
    ptw_ptecache_entry_t [PTW_CACHE_SIZE-1:0] ptecache_entry;
    logic [PTW_CACHE_SIZE-1:0] valid_vector;
    always_comb begin
        for (int i = 0; i < PTW_CACHE_SIZE; i++)
            valid_vector[i] = ptecache_entry[i].valid;
    end
endmodule
"""
        decl = bind_internals.infer_signal_declaration(rtl, 'ptecache_entry')
        self.assertIn('ptw_ptecache_entry_t', decl)
        self.assertIn('[PTW_CACHE_SIZE-1:0]', decl)
        self.assertNotIn('valid_vector', decl)
        self.assertNotIn('[i]', decl)

    def test_initialized_alias_keeps_packed_width(self):
        rtl = """\
module cic_decimator #(
    parameter int WIDTH     = 16,
    parameter int RMAX      = 2,
    parameter int N         = 2,
    parameter int REG_WIDTH = WIDTH + $clog2(RMAX ** N)
);
    logic [$clog2(RMAX+1)-1:0] cycle_reg;
    logic [REG_WIDTH-1:0] int_reg [0:N-1];
    logic [REG_WIDTH-1:0] comb_reg [0:N-1];
    logic [REG_WIDTH-1:0] int_reg_0 = int_reg[0];
    logic [REG_WIDTH-1:0] int_reg_1 = int_reg[1];
    logic [REG_WIDTH-1:0] comb_reg_0 = comb_reg[0];
    logic [REG_WIDTH-1:0] comb_reg_1 = comb_reg[1];
endmodule
"""
        self.assertEqual(
            bind_internals.infer_signal_declaration(rtl, 'comb_reg_0'),
            'input logic [REG_WIDTH-1:0] comb_reg_0',
        )
        self.assertEqual(
            bind_internals.infer_signal_declaration(rtl, 'int_reg_1'),
            'input logic [REG_WIDTH-1:0] int_reg_1',
        )
        self.assertEqual(
            bind_internals.infer_signal_declaration(rtl, 'cycle_reg'),
            'input logic [$clog2(RMAX+1)-1:0] cycle_reg',
        )
        self.assertEqual(
            bind_internals.infer_signal_declaration(rtl, 'int_reg'),
            'input logic [REG_WIDTH-1:0] int_reg [0:N-1]',
        )
        self.assertEqual(
            bind_internals.infer_signal_declaration(rtl, 'comb_reg'),
            'input logic [REG_WIDTH-1:0] comb_reg [0:N-1]',
        )

    def test_initialized_alias_is_promoted_with_dut_width(self):
        rtl = """\
module cic_decimator (
    input  logic clk,
    input  logic [15:0] input_tdata,
    output logic [17:0] output_tdata
);
    logic [17:0] comb_reg [0:1];
    logic [17:0] comb_reg_0 = comb_reg[0];
    assign output_tdata = comb_reg[1];
endmodule
"""
        graph = coi.build_signal_graph(rtl, 'cic_decimator')
        assertions = [
            'a_comb: assert property ((output_tdata == comb_reg_0))\n'
            'else begin\n    $display("x");\nend',
        ]
        _, prop, bind = bind_internals.apply_port_promotion(
            assertions,
            module_name='cic_decimator',
            graph=graph,
            rtl_text=rtl,
            prop_text=PROP.replace('tiny_prop', 'cic_decimator_prop'),
            bind_text=BIND.replace('tiny', 'cic_decimator').replace(
                'tiny_prop', 'cic_decimator_prop'),
        )
        self.assertIn('input logic [17:0] comb_reg_0', prop)
        self.assertNotIn('input logic comb_reg_0', prop)
        self.assertIn('.*', bind)

    def test_clog2_width_is_not_a_duplicate_port(self):
        prop = """\
module ptw_prop (
		input logic clk_i,
		input logic [$clog2(LEVELS)-1:0] count_q, // internal (density bind)
	);
"""
        names = bind_internals.existing_prop_ports(prop)
        self.assertIn('count_q', names)
        self.assertNotIn('LEVELS', names)
        self.assertNotIn('clog2', names)

    def test_verilog95_body_parameters_are_declared_on_the_checker(self):
        rtl = """\
module counter (
clk, reset_, count
);
    parameter width = 1;
    parameter min = 0;
    parameter [width:0] max = ((1<<width)-1);
    input clk;
    input reset_;
    input [width-1:0] count;
endmodule
"""
        prop = """\
module counter_prop
#(
		parameter ASSERT_INPUTS = 0)
 (
		input clk,
		input reset_,
		input [width-1:0] count
	);
"""
        bind = """\
bind counter counter_prop
	#(
		.ASSERT_INPUTS (0)
	) u_counter_sva(.*);
"""
        graph = coi.build_signal_graph(rtl, 'counter')
        _, new_prop, new_bind = bind_internals.apply_port_promotion(
            ['a_range: assert property ((count <= max))\n'],
            module_name='counter',
            graph=graph,
            rtl_text=rtl,
            prop_text=prop,
            bind_text=bind,
        )
        self.assertIn('parameter width = 1', new_prop)
        self.assertIn('parameter min = 0', new_prop)
        self.assertIn('parameter [width:0] max', new_prop)
        self.assertIn('.width (width)', new_bind)
        self.assertIn('.min (min)', new_bind)
        self.assertIn('.max (max)', new_bind)
        self.assertIn('.ASSERT_INPUTS (0)', new_bind)
        self.assertEqual(new_prop.count('parameter width = 1'), 1)

    def test_existing_checker_parameters_are_not_duplicated(self):
        rtl = """\
module alu_core #(parameter DATA_WIDTH = 32) (
    input logic [DATA_WIDTH-1:0] operand1
);
endmodule
"""
        prop = """\
module alu_core_prop
 #(
		parameter ASSERT_INPUTS = 0,
		parameter DATA_WIDTH = 32
)(
		input  logic [DATA_WIDTH-1:0] operand1
	);
"""
        bind = """\
bind alu_core alu_core_prop
	#(
		.ASSERT_INPUTS (0),
		.DATA_WIDTH (DATA_WIDTH)
	) u_alu_core_sva(.*);
"""
        graph = coi.build_signal_graph(rtl, 'alu_core')
        _, new_prop, new_bind = bind_internals.apply_port_promotion(
            ['a_x: assert property ((operand1 == operand1))\n'],
            module_name='alu_core',
            graph=graph,
            rtl_text=rtl,
            prop_text=prop,
            bind_text=bind,
        )
        self.assertEqual(new_prop.count('parameter DATA_WIDTH = 32'), 1)
        self.assertEqual(new_bind.count('.DATA_WIDTH (DATA_WIDTH)'), 1)

    def test_density_probe_tcl_has_no_check_fv(self):
        tcl = bind_internals.generate_density_probe_tcl(
            'tiny', module_type='sequential', clk_sig='clk_i', rst_sig='rst_ni')
        self.assertIn('report_assertion_density', tcl)
        self.assertIn('elaborate -sva $top', tcl)
        commands = [
            line for line in tcl.splitlines()
            if line.strip() and not line.lstrip().startswith('#')
        ]
        self.assertFalse(any('check_fv' in line for line in commands))
        self.assertIn('create_clock clk_i', tcl)


if __name__ == '__main__':
    unittest.main(verbosity=2)
