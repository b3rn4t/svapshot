# The Clear BSD License
# Copyright (c) [2021] [The Trustees of Princeton University]
# All rights reserved.

# Redistribution and use in source and binary forms, with or without
# modification, are permitted for academic and research use only (subject to the
# limitations in the disclaimer below) provided that the following conditions are met:
#      * Redistributions of source code must retain the above copyright notice,
#      this list of conditions and the following disclaimer.

#      * Redistributions in binary form must reproduce the above copyright
#      notice, this list of conditions and the following disclaimer in the
#      documentation and/or other materials provided with the distribution.

#      * Neither the name of the copyright holder nor the names of its
#      contributors may be used to endorse or promote products derived from this
#      software without specific prior written permission.

# NO EXPRESS OR IMPLIED LICENSES TO ANY PARTY'S PATENT RIGHTS ARE GRANTED BY
# THIS LICENSE. THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND
# CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
# PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
# BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER
# IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

# Ported into SVApshot as a native Python 3 harness scaffolder.
# Prefer: from scaffold import scaffold_harness


import argparse
import os
import re
import sys
from shutil import copyfile
from datetime import date


from scaffold.assertions import (  # noqa: E402
    assemble_assertion, escape_display_string, normalize_assertion_id,
    write_assert, write_assume)
from scaffold.tool_scripts import SKIP_LIB_SUBDIRS, should_skip_lib_subdir  # noqa: E402
import rtl_clocking  # noqa: E402

class ScaffoldError(Exception):
    """Harness scaffolding failed; ``returncode`` mirrors the old CLI exit status."""

    def __init__(self, message, returncode=1):
        super().__init__(message)
        self.returncode = returncode
        self.message = message


override_tool_script = 1
recursive = 0

IN = "--?IN>"
OUT = "--?OUT>"
implies = [IN, OUT]
# Port-list designer annotations: a `///` line, with an optional tag word.
_ANN = r"///\S*\s+"
# Arrays
params = []
interfaces = []
def_wires = []
assign_wires = {}
fields = {}
handshakes = {}
implications = {}
signals = {}
suffixes = {}
verbose = 0
clk_sig = "clk"
clk_edge = "posedge"
rst_sig = "rst_n"
clk_found = False
rst_found = False
rst_active_low = None   # None until something establishes the polarity
prop_filename_backup = ""

# The clock and reset rules live in rtl_clocking so that this script and main.py
# cannot answer the question differently.
from rtl_clocking import (looks_like_clock, EXACT_CLOCK_NAMES,  # noqa: E402
                          EXACT_RESET_NAMES, analyze_clocking,
                          reset_is_active_low_by_name)


def _better_match(current, candidate, found, exact_names):
    """True when candidate should replace the signal picked so far."""
    if not found:
        return True
    return (candidate.lower() in exact_names
            and current.lower() not in exact_names)


def adopt_body_clocking(dut_path):
    """Take the clock and reset from the module's clocked blocks if it has any.

    The port list cannot distinguish clk from clk_div_valid or clk_out, all
    three of which clock_divider_counter declares. Its always_ff blocks name the
    one the registers actually move on, and give the reset polarity outright.

    When the sensitivity list names an internal wire (``rst_n = arst_n ^ ...``),
    the port list is passed through so ``analyze_clocking`` can prefer a real
    primary input for the property module and ``create_reset``.
    """
    global clk_sig, clk_edge, rst_sig, clk_found, rst_found, rst_active_low
    try:
        handle = open(dut_path, "r")
        text = handle.read()
        handle.close()
    except IOError:
        return

    ports = re.findall(
        r"\b(?:input|output|inout)\b"
        r"(?:\s+(?:wire|reg|logic|signed|unsigned))*"
        r"\s*(?:\[[^\]]*\])?\s*(\w+)",
        text)
    info = analyze_clocking(text, ports)
    if not info.clock:
        return   # purely structural: the port names are all there is

    if clk_found and info.clock != clk_sig:
        print("Note: clock taken from the module's clocked blocks: " + info.clock
              + " (the port list suggested " + clk_sig + ")")
    clk_sig = info.clock
    clk_edge = info.clock_edge or clk_edge
    clk_found = True

    if info.reset:
        if rst_found and info.reset != rst_sig:
            print("Note: reset taken from the module's clocked blocks: " + info.reset
                  + " (the port list suggested " + rst_sig + ")")
        rst_sig = info.reset
        rst_active_low = info.reset_active_low
        rst_found = True

def get_reset():
    # An asynchronous reset states its polarity in the sensitivity list. Only
    # when nothing has established it do we fall back to reading the name.
    active_low = rst_active_low
    if active_low is None:
        active_low = reset_is_active_low_by_name(rst_sig)
    return ("!" + rst_sig) if active_low else rst_sig
def check_size(name, sub_size, size, params):
    if sub_size and sub_size not in params:
        print("Warning: Signal '" + name + "' has a size of '" + size + "' that is not defined in this module!")

def check_interface(name, interfaces):
    if name not in interfaces:
        print("Error: '" + name + "' is not a valid interface!")
        return 1
    return 0

def check_signal(name, signals):
    if name not in signals:
        print("Error: '" + name + "' is not a valid signal!")
        return 1
    return 0

def check_annotation(annotation, line):
    if not annotation:
        print("Error: Invalid signal '" + line.replace("\n", "") + "' for annotation!")
        sys.exit(1)

def check_size_match(name, size1, size2):
    if (size1 != size2):
        print("Error: Annotation '" + name + "' has mismatched sizes! "+str(size1)+" and "+str(size2))
        return 1
    return 0

def check_id(name, entry):
    if not "p_id" in entry or not "q_id" in entry:
        print("Error: Annotation '" + name + "' is missing a main transaction ID!")
        return 1
    return 0

def check_trans_id(name, entry):
    if not "p_trans_id" in entry or not "q_trans_id" in entry:
        print("Error: Annotation '" + name + "' is missing a transaction ID!")
        return 1
    return 0

def create_dir(name):
    if (not os.path.exists(name)):
        try:
            os.makedirs(name)
        except OSError:
            print ("Creation of the directory %s failed" % name)
            sys.exit(1)
        else:
            print ("Successfully created the directory %s" % name)

def parse_args():
  parser = argparse.ArgumentParser(description='SVApshot formal harness scaffolder.')
  parser.add_argument("-f", "--filename", required=True, type=str, help="Path to DUT RTL module file.")
  parser.add_argument("-src", "--source", nargs="+", type=str, default=[], help="Path to source code folder where submodules are.")
  parser.add_argument("-i", "--include", type=str, help="Path to include folder that DUT and submodules use.")
  parser.add_argument("-as", "--submodule_assert", nargs="+", type=str, default=[], help="List of submodules for which ASSERT the behavior of outgoing transactions.")
  parser.add_argument("-am", "--submodule_assume", nargs="+", type=str, default=[], help="List of submodules for which ASSUME the behavior of outgoing transactions (ASSUME can constrain the DUT).")
  parser.add_argument("-x", "--xprop_macro", nargs="?", type=str, const = "NONE", help="Generate X Propagation assertions, specify argument to create property under <MACRO> (default none)")
  parser.add_argument("-tool", nargs="?", type=str, const = "jasper", help="Backend tool to use [jasper|sby] (default jasper)")
  parser.add_argument("-v", "--verbose", action='store_true', help="Add verbose output.")
  parser.add_argument("-mt", "--module-type", choices=['sequential', 'combinational'], default='sequential', help="Type of RTL module (sequential or combinational)")
  args = parser.parse_args()
  return args

def clean(line):
    line = line.replace("\t", "")
    while line.startswith(" "):
        line = line[1:]
    return line

def abort_tool(prop):
    print("ABORTING!")
    if not prop.closed:
        prop.close()
    copyfile(prop_filename_backup, prop.name)
    sys.exit(1)

def parse_annotation(line):
    global implications
    global def_wires,assign_wires

    name = None
    symbol = None

    match_in = re.search("(?:"+_ANN+")?(\w+)\s*:\s*(\w+)\s*" + IN + "\s*(\w+)", line)
    match_out = re.search("(?:"+_ANN+")?(\w+)\s*:\s*(\w+)\s*" + OUT + "\s*(\w+)", line)
    match_wire = re.search("(?:"+_ANN+")?(\[.*:0\]\s+)?(\w+)\s*[=]\s*(.+)", line)

    match_trans_id = re.search(_ANN+"trans_id", line)
    match_trans_id_other = re.search(_ANN+r"(trans_id_\d+)", line)
    match_trans_id_unique = re.search(_ANN+"trans_id_unique", line)
    match_trans_id_other_unique = re.search(_ANN+r"(trans_id_\d+_unique)", line)
    match_data = re.search(_ANN+"data", line)

    if (verbose):  print("Parsing annotation:" + line[:-1])
    #check_annotation((match_in or match_out or match_trans_id or
    #                match_trans_id_other or match_data or match_wire), line)
    if match_in:
        name = match_in.group(1)
        p = match_in.group(2)
        q = match_in.group(3)
        symbol = IN
    elif match_out:
        name = match_out.group(1)
        p = match_out.group(2)
        q = match_out.group(3)
        symbol = OUT
    elif match_trans_id_other_unique:
        name = match_trans_id_other_unique.group(1)
        return name
    elif match_trans_id_unique:
        return "trans_id_unique"
    elif match_trans_id_other:
        name = match_trans_id_other.group(1)
        return name
    elif match_trans_id:
        if (verbose):  print("Matched trans_id")
        return "trans_id"
    elif match_data:
        if (verbose):  print("Matched data")
        return "data"
    elif match_wire:
        size = match_wire.group(1)
        name = match_wire.group(2)
        assign = match_wire.group(3)

        if not assign.endswith(";"):
            assign += ";"
        if size:
            wire = "wire "+size+name+";"
        else:
            wire = "wire "+name+";"

        def_wires.append(wire)
        assign_wires[name]=assign
        if (verbose):  print("Matched wire:" + wire +", assign: "+assign)
        parse_signal(wire+"\n",None)


    #add implications
    if symbol in implies:
        annotation = {"p": p, "q": q, "symbol": symbol, "size": "", "p_unique": None, "q_unique": None}
        implications[name] = annotation
        if (verbose):  print("Matched iface:" + p +", and: "+q)
    return None

def parse_signal(line, annotation):
    global interfaces, handshakes, signals, verbose, rst_sig, clk_sig
    global clk_found, rst_found
    name = None
    entry = None
    val = None
    rdy = None

    if (verbose):  print("Parsing signal: " + line[:-1])
    match = re.search("\s*[wire|logic|reg]?\s+(?:\[(.*):0\]\s+)?(\w+)\s*[,;=]?\s*(//.*)?\n", line)
    if match:
        size = match.group(1)
        name = match.group(2)
        comment = match.group(3)
        if size is None:
            size = "0"
        else:
            size = size.replace(" ","")
        #check_size(name, sub_size, size, params)
        if (verbose):  print("Parsed signal, name: " + str(name) + ", size: " + str(size) + ", com: " + str(comment))

        if looks_like_clock(name):
            if _better_match(clk_sig, name, clk_found, EXACT_CLOCK_NAMES):
                clk_sig = name
            clk_found = True
        elif name.startswith("rst") or name.startswith("reset") or name.endswith("reset"):
            if _better_match(rst_sig, name, rst_found, EXACT_RESET_NAMES):
                rst_sig = name
            rst_found = True
        #if it is a valid or ready signal
        elif name.endswith("_val"):
            val = name.replace("_val", "")
            if val not in interfaces: # val sees first
                interfaces.append(val)
                handshakes[val] = val
            else: # got added by rdy, need to find match
                handshakes[val] = handshakes[val].replace("//", "")
        elif name.endswith("_res") or name.endswith("_rdy") or name.endswith("_ack"):
            rdy = name[0:len(name)-4]
            if rdy not in interfaces: # rdy sees first
                interfaces.append(rdy)
                handshakes[rdy] = name + "//" # yet to find matching val
            else: # got added by val
                handshakes[rdy] = name
        elif name.endswith("_trans_id") or name.endswith("_transid"):
            annotation = "trans_id"
        elif name.endswith("_trans_id_unique") or name.endswith("_transid_unique"):
            annotation = "trans_id_unique"
        # add only if the interface is defined (in implications)
        elif name.endswith("_stable"):
            annotation = "stable"
        elif name.endswith("_data"):
            key = name.split(" ")[-1]
            key = key[:-5] #remove last 5 chars
            # if key in handshakes:
            annotation = "data"
        elif name.endswith("_active"):
            key = name.split(" ")[-1]
            key = key[:-7]
            if key in implications:
                struct = implications[key]
                if (struct): struct["active"] = name

        #if the signal was annotated
        if annotation:
            fields[name] = annotation
            if (verbose):  print("Annotated as: " + annotation)

        entry = {"size": size, "comment": comment}
        signals[name] = entry

def parse_fields(prop):
    options = "("
    list = []
    for impl_name in implications:
        entry = implications[impl_name]
        list.append(entry["p"])
        list.append(entry["q"])
    list.sort(reverse=True)
    for name in list:
        options+=name+"|"
    options+="ERROR)"

    ##fields are annotated signals
    for signal_name in fields:
        #separate iface_name and field_name (annotation)
        annotation_match = re.search(options+"_(\w+)", signal_name)
        if (check_annotation(annotation_match, signal_name)):
            abort_tool(prop)
        interface = annotation_match.group(1)
        suffix = annotation_match.group(2)
        if (verbose):  print("PF iface: " + interface + ", suffix: " + suffix)
        # Append the to interface entry of the dict, the suffix
        if (not interface in suffixes):
            suffixes[interface] = []
        suffixes[interface].append(suffix)

        #field is the type of annotation
        field = fields[signal_name]
        for impl_name in implications:
            #print("PF implication_name: " + str(impl_name));
            entry = implications[impl_name]
            if entry["p"] == interface:
                update_entry(field, impl_name, "p_", suffix)
            if entry["q"] == interface:
                update_entry(field, impl_name, "q_", suffix)

def update_entry(field, impl_name, type, suffix):
    global implications
    if (verbose):  print("UA field: " + field + ", impl_name: " + impl_name + ", type: " + type + ", suffix: " + suffix)
    entry = implications[impl_name]
    if field == "trans_id" or field == "trans_id_unique":
        entry[type + "id"] = suffix
        entry[type + field] = [suffix, ""]
    elif "trans_id" in field:
        entry[type + field] = [suffix, ""]
    elif field == "data" or field == "stable":
        entry[type + field] = suffix

    if "unique" in field:
        entry[type + field] = [suffix, ""]

    if (verbose):  print("UA: " + str(entry));

def get_sizes(prop):
    global implications,signals
    for impl_name in implications:
        entry = implications[impl_name]
        #if (check_id(impl_name, entry)): abort_tool(prop)
        if not "p_id" in entry:
            name = entry["p"]+"_transid"
            signals[name] = {"size": "'0", "comment": ""}
            entry["p_id"] = "transid"
            entry["p_trans_id"] = ["transid", ""]
        if not "q_id" in entry:
            name = entry["q"]+"_transid"
            signals[name] = {"size": "'0", "comment": ""}
            entry["q_id"] = "transid"
            entry["q_trans_id"] = ["transid", ""]
        if entry["symbol"] in implies:
            if check_interface(entry["p"], interfaces) or check_interface(entry["q"], interfaces):
                abort_tool(prop)
            for key in entry:
                keyq = key.replace("p", "q")
                if ("p_trans_id" in key) and (keyq in entry):
                    #trans_id signal suffix
                    p_trans_id = entry[key][0]
                    q_trans_id = entry[keyq][0]
                    #full signal name with iface prefix
                    p_signal_name = entry["p"] + "_" + p_trans_id
                    q_signal_name = entry["q"] + "_" + q_trans_id
                    if (verbose):  print("GS p: " + p_signal_name + ", q: " + q_signal_name);
                    #check that signals exist in signal dict
                    if check_signal(p_signal_name, signals) or check_signal(q_signal_name, signals):
                        abort_tool(prop)
                    #get the size of p and q trans_id from signals dict
                    size_p = signals[p_signal_name]["size"]
                    size_q = signals[q_signal_name]["size"]
                    #check that sizes match
                    if check_size_match(impl_name, size_p, size_q):
                        abort_tool(prop)
                    #update size in entries
                    entry[key][1] = size_p
                    entry[keyq][1] = size_q
                    if entry["p_id"] == p_trans_id:
                        entry["size"] = size_p
        if (verbose):  print("GS: " + str(entry));

def close_bind(module_name, bind, extra_params=None):
    if getattr(bind, "closed", False):
        return
    for name in extra_params or []:
        bind.write("\t\t." + name + " (" + name + "),\n")
    bind.write("\t\t.ASSERT_INPUTS (0)\n")
    bind.write("\t) u_" + module_name + "_sva(.*);")
    bind.close()


def _parse_body_parameter_line(line):
    """Return ``(name, declaration)`` for a Verilog-95 body parameter, or None.

    Port widths such as ``[width-1:0]`` need that name declared on the checker
    ``#()`` list. Body parameters are not ports, so they are collected and
    emitted there rather than into the ANSI port list.
    """
    stripped = line.lstrip().rstrip("\n")
    if "//" in stripped:
        stripped = stripped.split("//", 1)[0].rstrip()
    if stripped.endswith(";") or stripped.endswith(","):
        stripped = stripped[:-1].rstrip()
    if not re.match(r"(?:parameter|localparam)\b", stripped):
        return None
    equals = stripped.find("=")
    if equals < 0:
        return None
    name_match = re.search(r"([A-Za-z_]\w*)\s*$", stripped[:equals])
    if not name_match:
        return None
    return name_match.group(1), stripped


def _inject_body_params_into_open_prop(prop, body_params):
    """Insert collected body parameters ahead of ``ASSERT_INPUTS``."""
    if not body_params:
        return
    prop.flush()
    prop.seek(0)
    text = prop.read()
    extra = "".join("\t\t" + decl + ",\n" for _name, decl in body_params)
    needle = "parameter ASSERT_INPUTS = 0"
    index = text.find(needle)
    if index < 0:
        return
    text = text[:index] + extra + "\t\t" + text[index:]
    prop.seek(0)
    prop.truncate()
    prop.write(text)


def _finish_deferred_bind(prop, bind, module_name, body_params):
    """Copy body parameters onto the checker and close the bind file."""
    global params
    _inject_body_params_into_open_prop(prop, body_params)
    extra_names = []
    for name, _decl in body_params:
        if name not in params:
            params.append(name)
        extra_names.append(name)
    close_bind(module_name, bind, extra_names)

def gen_disclaimer(prop,rtl_file_relation):
    #license_file = open(os.getcwd()+"/LICENSE", "r")
    if rtl_file_relation:
        prop.write("// This property file was autogenerated by SVApshot on "+str(date.today())+"\n")
        prop.write("// to check the behavior of the original RTL module, whose interface is described below: \n")
    #license_file.close()

def _close_prop_ports(prop):
    """Finish the property-module port list and open the local-parameters section."""
    parse_fields(prop)
    get_sizes(prop)
    prop.write("\t);\n")
    prop.write("\n")
    prop.write("//==============================================================================\n")
    prop.write("// Local Parameters\n")
    prop.write("//==============================================================================\n\n")


def _mirror_port_line(prop, line, annotation):
    """Write one DUT port declaration into the property module as an input."""
    if line.startswith("output"):
        line = line.replace("output", "input ", 1).split("\n")[0]
        line += " //output\n"
        prop.write("\t\t" + line)
        parse_signal(line[5:], annotation)
    elif line.startswith("input"):
        prop.write("\t\t" + line)
        parse_signal(line[5:], annotation)
    elif line.startswith("inout"):
        # Observe both directions; the DUT still owns the net.
        line = line.replace("inout", "input ", 1)
        prop.write("\t\t" + line)
        parse_signal(line[5:], annotation)
    elif line.startswith("wire"):
        prop.write("\t\tinput  " + line)
        parse_signal(line, annotation)
    else:
        prop.write("\t\t" + line)


def _body_port_as_input(line):
    """Turn a Verilog-95 body port declaration into an ANSI ``input`` line.

    Body declarations end with ``;``. The property module uses an ANSI port
    list, so the terminator becomes a comma (stripped again for the last
    port when the list is flushed).
    """
    stripped = line.lstrip().rstrip("\n")
    comment = ""
    if "//" in stripped:
        stripped, comment = stripped.split("//", 1)
        comment = "//" + comment
        stripped = stripped.rstrip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    if stripped.startswith("output"):
        stripped = "input " + stripped[len("output"):].lstrip()
        if not comment:
            comment = "//output"
        elif "output" not in comment:
            comment = comment + " //output"
    elif stripped.startswith("inout"):
        stripped = "input " + stripped[len("inout"):].lstrip()
    elif stripped.startswith("wire"):
        stripped = "input  " + stripped
    # Drop a body ``reg``/``wire`` after the direction: illegal in an input port.
    stripped = re.sub(r"^(input\s+)(?:reg|wire)\b\s*", r"\1", stripped)
    return stripped, comment


def _flush_body_ports(prop, body_ports_buf):
    """Write buffered body ports as an ANSI list, then close the port section."""
    last = len(body_ports_buf) - 1
    for index, (decl, comment) in enumerate(body_ports_buf):
        sep = "" if index == last else ","
        suffix = (" " + comment) if comment else ""
        prop.write("\t\t" + decl + sep + suffix + "\n")
        # parse_signal expects the declaration after the direction keyword.
        payload = decl
        if payload.startswith("input"):
            payload = payload[5:]
        parse_signal(payload + "\n", None)
    _close_prop_ports(prop)


def _ends_port_list(line):
    """True when ``line`` closes the module port list.

    ANSI headers often put ``);`` alone on a line. Verilog-95 non-ANSI headers
    put it after the last bare name (``sda_padoen_o );``). Matching only a
    line that *starts* with ``);`` lets the body leak into the property module.
    """
    return ");" in line


def _body_decl_ends(line):
    """True when a non-ANSI body line is past the port/parameter declarations.

    ``reg`` / ``wire`` redeclarations of ports are skipped by the caller and
    must not end the scan: OpenCores modules interleave them with later
    ``input``/``output`` lines (I2C declares ``reg wb_dat_o`` before
    ``input scl_pad_i``).
    """
    stripped = line.lstrip()
    if not stripped or stripped.startswith("//") or stripped.startswith("/*"):
        return False
    if stripped.startswith("`"):
        return False
    starters = (
        "always", "assign", "initial", "function", "task", "generate",
        "endmodule",
    )
    for starter in starters:
        if stripped.startswith(starter):
            return True
    # Module instantiation: identifier followed by optional #() then instance.
    if re.match(r"[A-Za-z_]\w*\s+(?:#\s*\(|[A-Za-z_]\w*\s*\()", stripped):
        return True
    return False


def parse_global(rtl, prop, bind):
    global params

    is_param = False
    is_signal = False
    body_ports = False
    body_ports_buf = []
    body_params = []
    defer_bind_close = False
    header_ports_closed = False
    annotation_scope = False
    parsed_module_sec = False
    parsed_disclaimer_sec = False
    seen_typed_port = False
    module_name = "error"
    annotation = None

    for line in rtl:
        line = clean(line)
        #Show the parsed line if verbose mode
        if (verbose):  print("Parsing global:" + line[:-1])

        if body_ports:
            # Verilog-95: directions live in the body. Mirror only those, then
            # stop before always/assign/instances so the RTL body never becomes
            # illegal ports in the SystemVerilog checker.
            stripped = line.lstrip()
            if (stripped.startswith("input") or stripped.startswith("output")
                    or stripped.startswith("inout")):
                if not header_ports_closed:
                    seen_typed_port = True
                    body_ports_buf.append(_body_port_as_input(stripped))
                annotation = None
            elif stripped.startswith("parameter") or stripped.startswith("localparam"):
                # Body parameters belong after the port list, not inside it.
                # Copy them onto the checker #() so [width-1:0] elaborates.
                parsed = _parse_body_parameter_line(stripped)
                if parsed:
                    body_params.append(parsed)
            elif _body_decl_ends(line):
                if defer_bind_close:
                    _finish_deferred_bind(prop, bind, module_name, body_params)
                    defer_bind_close = False
                if not header_ports_closed:
                    _flush_body_ports(prop, body_ports_buf)
                body_ports = False
                break
            continue

        if line.startswith("module"):
            if not parsed_disclaimer_sec:
                parsed_disclaimer_sec = True
                gen_disclaimer(prop,True)
            parsed_module_sec = True
            matches = re.search("module\s+(\w+)(.*)", line)
            module_name = matches.group(1)
            print("Module name:" + module_name)
            rest = matches.group(2)
            prop.write("module " + module_name + "_prop\n")
            gen_disclaimer(bind,False)
            bind.write("bind " + module_name + " " + module_name + "_prop\n")
            bind.write("\t#(\n")
            if "#(" in rest:
                is_param = True
                prop.write(rest+"\n\t\tparameter ASSERT_INPUTS = 0,\n")
            elif "(" in rest:
                is_signal = True
                # Verilog-95 body parameters arrive after ");". Close the bind
                # once those declarations have been collected.
                defer_bind_close = True
                if ");" in rest:
                    before = rest.split(");", 1)[0]
                    typed = bool(re.search(r"\b(?:input|output|inout)\b", before))
                    if typed:
                        prop.write(
                            "#(\n\t\tparameter ASSERT_INPUTS = 0)\n" + rest + "\n")
                        header_ports_closed = True
                    else:
                        # Bare names on the module line: directions come later.
                        prop.write("#(\n\t\tparameter ASSERT_INPUTS = 0)\n(\n")
                    body_ports = True
                else:
                    prop.write(
                        "#(\n\t\tparameter ASSERT_INPUTS = 0)\n" + rest + "\n")

        elif (is_param and (not is_signal)) or (parsed_module_sec and line.startswith("#(")):
            if line.startswith("#("):
                is_param = True
                line = line[2:]
                prop.write("#(\n\t\tparameter ASSERT_INPUTS = 0,\n")
            if "parameter" in line or "localparam" in line:
                prop.write("\t\t" + line)
                # Match both parameter and localparam declarations
                matches = re.findall("\s*\t*(?:parameter|localparam)\s+(\w+)\s+=\s*\d+(?:,)?", line)
                for param in matches:
                    bind.write("\t\t." + param + " (" + param + "),\n")
                    params.append(param)
            #when param section contains the close symbol
            elif line.startswith(")"):
                prop.write(line)
                is_signal = True
                close_bind(module_name,bind)
            #else:
            #    print("Wrong format in parameter section! Expecting parameter or )")

        elif is_signal or (parsed_module_sec and line.startswith("(")):
            if line.startswith("("):
                if not is_signal:
                    is_signal = True
                    if is_param:
                        close_bind(module_name, bind)
                    else:
                        defer_bind_close = True
                if (not is_param):
                    prop.write("#(\n\t\tparameter ASSERT_INPUTS = 0)\n(\n")
                else:
                    prop.write("(")
            elif re.match(r"/\*\S", line):
                annotation_scope = True
            elif line.startswith("*/"):
                annotation_scope = False
            elif line.startswith("///") or annotation_scope: # annotation
                annotation = parse_annotation(line)
            else:
                if _ends_port_list(line):
                    before = line.split(");", 1)[0].strip()
                    if before:
                        # Last ANSI port on the same line as ");".
                        if (before.startswith("input") or before.startswith("output")
                                or before.startswith("inout") or before.startswith("wire")):
                            seen_typed_port = True
                            _mirror_port_line(prop, before + "\n", annotation)
                        elif seen_typed_port:
                            prop.write("\t\t" + before + "\n")
                        # else: bare Verilog-95 name - directions come from the body
                    if seen_typed_port:
                        if defer_bind_close:
                            _finish_deferred_bind(
                                prop, bind, module_name, body_params)
                            defer_bind_close = False
                        _close_prop_ports(prop)
                        break
                    # Non-ANSI: keep the port list open and read body directions.
                    body_ports = True
                    annotation = None
                    continue
                if (line.lstrip().startswith("input")
                        or line.lstrip().startswith("output")
                        or line.lstrip().startswith("inout")
                        or line.lstrip().startswith("wire")):
                    seen_typed_port = True
                    _mirror_port_line(prop, line.lstrip(), annotation)
                elif seen_typed_port:
                    prop.write("\t\t" + line)
                # else: buffer bare non-ANSI names by not writing them
                annotation = None

        elif not parsed_module_sec:
            if (not parsed_disclaimer_sec) and (not line.startswith("//")):
                parsed_disclaimer_sec = True
                gen_disclaimer(prop,True)
            if parsed_disclaimer_sec:
                # Do not copy `include / timescale into the SV checker: the
                # DUT already has them, and a missing include path breaks
                # analysis of an otherwise valid property module.
                stripped = line.lstrip()
                if (stripped.startswith("`include")
                        or stripped.startswith("`timescale")
                        or stripped.startswith("`def")
                        or stripped.startswith("`if")):
                    pass
                else:
                    prop.write(line)
        else:
            print("Unexpected line after module header parsed")
            #sys.exit(1)
    if body_ports:
        if defer_bind_close:
            _finish_deferred_bind(prop, bind, module_name, body_params)
            defer_bind_close = False
        if not header_ports_closed:
            _flush_body_ports(prop, body_ports_buf)
    elif defer_bind_close:
        _finish_deferred_bind(prop, bind, module_name, body_params)
    return module_name

def gen_vars(tool, prop, module_type='sequential'):
    if module_type == 'sequential':
        # Falling back to the default names would put the clocking block on a
        # signal the property module never declares, so the file could not
        # elaborate. Refuse instead of writing a testbench that cannot be used.
        if not clk_found:
            print("Error: a sequential testbench was requested but no clock port was")
            print("       found in the module interface. Ports considered clocks are")
            print("       named clk*/clock* or *clk/*clock/*clk_i/*clock_i.")
            print("       Re-run with -mt combinational if the module has no clock.")
            # Leave no half-written property file behind for a later stage to
            # pick up; any designer-added SVA is already in <module>_prop_old.sv.
            prop.close()
            try:
                os.remove(prop.name)
            except OSError:
                pass
            raise ScaffoldError(
                'a sequential testbench was requested but no clock port was '
                'found in the module interface. Re-run with -mt combinational '
                'if the module has no clock.')
        prop.write("genvar j;\ndefault clocking cb @(" + clk_edge + " " + clk_sig + ");\nendclocking\n")
        if rst_found:
            prop.write("default disable iff (" + get_reset() + ");\n")
        else:
            # No reset port: a disable condition on an undeclared signal would
            # be just as fatal, and no condition at all is valid SystemVerilog.
            print("Warning: no reset port found; the property file will have no")
            print("         'default disable iff' clause.")
    else:
        # Concurrent assertions cannot be unclocked (VCF NCIFA). The DUT has
        # no clock, so the checker owns a sampling net. Designer assertions
        # stay `assert property ((expr))` without an `@(...)` event.
        prop.write("genvar j;\n")
        prop.write("logic " + rtl_clocking.FORMAL_CLK + ";\n")
        prop.write(
            "default clocking cb @(posedge " + rtl_clocking.FORMAL_CLK
            + ");\nendclocking\n")
    if tool == "sby" and module_type == 'sequential':
        prop.write("reg reset_r = 0;\n")
        write_assume(prop, "rst", "reset_r != " + get_reset(), indent="\t")
        prop.write("always_ff @(" + clk_edge + " " + clk_sig + ")\n")
        prop.write("    reset_r <= 1'b1;\n")
    prop.write("\n// Re-defined wires \n")
    # Keep track of the wires already defined for symbolics and handshakes
    def_symb_hsk = []
    for line in def_wires:
        prop.write(line+"\n")
    prop.write("\n// Symbolics and Handshake signals\n")
    for impl_name in implications:
        entry = implications[impl_name]
        for key in entry:
            if "p_trans_id" in key and key.replace("p", "q") in entry:
                if (verbose):  print("GenVAR:" + impl_name +", key "+key)
                trans_id_entry = entry[key]
                size = trans_id_entry[1]
                symb_name = "symb_" + entry["p"] + "_" + trans_id_entry[0]
                if size != "'0" and (not symb_name in def_symb_hsk):
                    prop.write("wire [" + size + ":0] " + symb_name +";\n")
                    write_assume(prop, symb_name + "_stable", "$stable(" + symb_name + ")", indent="\t")
                    def_symb_hsk.append(symb_name)
            elif key == "p" or key == "q":
                interface = entry[key]
                handshake = handshakes[interface]
                if handshake.endswith("//"): # skip those that never found matching val
                    continue
                # Define Handshake wire
                base = handshake
                wire_def = base + "_hsk"
                if interface != handshake: # valid and ready
                    base = handshake[0:len(handshake)-4]
                    wire_def = base + "_hsk"

                # Check if the wire was defined already
                if not wire_def in def_symb_hsk:
                    def_symb_hsk.append(wire_def)
                    if interface != handshake:
                        prop.write("wire " + base + "_hsk = " + base + "_val && " + handshake + ";\n")
                    else: # Valid without a matching ready
                        prop.write("wire " + wire_def + " = " + base + "_val;\n")

    prop.write("\n")

def gen_models(prop):
    prop.write("//==============================================================================\n")
    prop.write("// Modeling\n")
    prop.write("//==============================================================================\n\n")
    for iface_name in implications:
        entry = implications[iface_name]
        if entry["symbol"] == IN:
            gen_in(prop, iface_name, entry)
        elif entry["symbol"] == OUT:
            gen_out(prop, iface_name, entry)

        for key in entry:
            if key.startswith("p") and "unique" in key and key.replace("p", "q") in entry:
                if entry[key] and entry[key.replace("p", "q")]:
                    gen_unique(prop, iface_name, entry, entry[key], entry[key.replace("p", "q")])

def gen_in(prop, name, entry):
    p = entry["p"]
    q = entry["q"]
    size = entry["size"]
    if (verbose):
        print("GenIN:" + name)

    prop.write("// Modeling incoming request for " + name + "\n")
    if q != handshakes[q]: # has ready signal, not equal to interface name
        # the external module eventually accepts the response
        prop.write("if (ASSERT_INPUTS) begin\n")
        write_assert(prop, name + "_fairness", q + "_val |-> s_eventually(" + handshakes[q] + ")", indent="\t")
        prop.write("end else begin\n")
        write_assume(prop, name + "_fairness", q + "_val |-> s_eventually(" + q + "_rdy)", indent="\t")
        prop.write("end\n\n")

    key = "p_trans_id"; keyq = "q_trans_id"
    if key in entry and keyq in entry:
        name_tid = name + "_" +entry[key][0]
        p_trans_id = p + "_" + entry[key][0]
        q_trans_id = q + "_" + entry[keyq][0]
        symb_name = "symb_" + p_trans_id

        if (verbose):  print("GenIN Trans:" + p_trans_id)
        prop.write("// Generate sampling signals and model\n")
        count = 1
        if not "unique" in key:
            count = 3 #count_log-1
        gen_iface_sampled(prop, name_tid, p, q, p_trans_id, q_trans_id, symb_name, entry, count,size)

        # Generate Stability Assumptions
        # If stable then assume payload is stable and valid is non-dropping
        if p != handshakes[p] and "p_stable" in entry:
            prop.write("// Assume payload is stable and valid is non-dropping\n")
            prop.write("if (ASSERT_INPUTS) begin\n")
            write_assert(prop, name_tid + "_stability", p + "_val && !" + handshakes[p] + " |=> "+ p +"_val && $stable(" + p + "_" + entry["p_stable"] + ") ", indent="\t")
            prop.write("end else begin\n")
            write_assume(prop, name_tid + "_stability", p + "_val && !" + handshakes[p] + " |=> "+ p +"_val && $stable(" + p + "_" + entry["p_stable"] + ") ", indent="\t")
            prop.write("end\n\n")

        # Otherwise assert that if valid eventually ready or dropped valid
        if p != handshakes[p]:
            prop.write("// Assert that if valid eventually ready or dropped valid\n")
            write_assert(prop, name_tid + "_hsk_or_drop", p + "_val |-> s_eventually(!" + p + "_val || "+handshakes[p]+")")
        prop.write("// Assert that every request has a response and that every reponse has a request\n")
        eventual_expr = "|" + name_tid + "_sampled |-> s_eventually(" + q + "_val"
        if size != "'0":
            eventual_expr += " && (" + q_trans_id + " == " + symb_name + ") )"
        else:
            eventual_expr += ")"
        write_assert(prop, name_tid + "_eventual_response", eventual_expr)
        write_assert(prop, name_tid + "_was_a_request", name_tid + "_response |-> "+name_tid+"_set || "+name_tid+"_sampled")
        prop.write("\n")
    if "p_data" in entry and "q_data" in entry:
        p_trans_id = p + "_" + entry["p_id"]
        q_trans_id = q + "_" + entry["q_id"]
        name_tid = name + "_" + entry["p_id"]
        p_data = p + "_" + entry["p_data"]
        q_data = q + "_" + entry["q_data"]
        symb_name = "symb_" + p_trans_id
        data_integrity(prop, name_tid, p, q, p_trans_id, q_trans_id, p_data, q_data, symb_name, size)

def gen_iface_sampled (prop, name, p, q, p_trans_id, q_trans_id, symb_name, entry, count,size):
    prop.write("reg ["+str(count)+":0] " + name + "_sampled;\n")
    prop.write("wire " + name + "_set = " + p + "_hsk")
    if size != "'0": prop.write(" && " + p_trans_id + " == " + symb_name + ";\n")
    else: prop.write(";\n")
    prop.write("wire " + name + "_response = " + q + "_hsk")
    if size != "'0": prop.write(" && " + q_trans_id + " == " + symb_name + ";\n\n")
    else: prop.write(";\n\n")

    prop.write("always_ff @(" + clk_edge + " " + clk_sig + ") begin\n")
    prop.write("\tif(" + get_reset() + ") begin\n")
    prop.write("\t\t" + name + "_sampled <= '0;\n")
    prop.write("\tend else if (" + name + "_set || "+ name + "_response ) begin\n")
    prop.write("\t\t"+name+"_sampled <= "+name+"_sampled + "+name+"_set - "+name+"_response;\n")
    prop.write("\tend\n")
    prop.write("end\n")
    # do not create sampled cover if it's never going to be sampled
    p_rdy = p+"_rdy";
    p_val = q+"_val"
    if not ((p_rdy in assign_wires) and (p_val in assign_wires) and (assign_wires[p_rdy] == assign_wires[p_val]) ):
        prop.write("co__" + name + "_sampled: cover property (|" + name + "_sampled);\n")
    if count > 0: # When not unique assume that this sampling structure would not overflow
        prop.write("if (ASSERT_INPUTS) begin\n")
        write_assert(prop, name + "_sample_no_overflow", name+"_sampled != '1 || !"+name+"_set", indent="\t")
        prop.write("end else begin\n")
        write_assume(prop, name + "_sample_no_overflow", name+"_sampled != '1 || !"+name+"_set", indent="\t")
        prop.write("end\n\n")

    if "active" in entry:
        write_assert(prop, name + "_active", name + "_sampled > 0 |-> "+entry["active"])
        prop.write("\n")
    else:
        prop.write("\n")

def data_integrity(prop, name, p, q, p_trans_id, q_trans_id, p_data, q_data, symb_name, size):
    size_p = signals[p_data]["size"]

    prop.write("\n// Modeling data integrity for " + name + "\n")
    prop.write("reg [" + size_p + ":0] " + name + "_data_model;\n")
    prop.write("always_ff @(" + clk_edge + " " + clk_sig + ") begin\n")
    prop.write("\tif(" + get_reset() + ") begin\n")
    prop.write("\t\t" + name + "_data_model <= '0;\n")
    prop.write("\tend else if (" + name + "_set) begin\n")
    prop.write("\t\t" + name + "_data_model <= " + p_data + ";\n")
    prop.write("\tend\n")
    prop.write("end\n\n")
    write_assert(prop, name + "_data_unique", "|" + name + "_sampled |-> !"+name+"_set")
    write_assert(prop, name + "_data_integrity", "|" + name + "_sampled && "+name+"_response |-> (" + q_data + " == " + name + "_data_model)")
    prop.write("\n")

def gen_out(prop, name, entry):
    p = entry["p"]
    q = entry["q"]
    size = entry["size"]
    p_trans_id = p + "_" + entry["p_id"]
    q_trans_id = q + "_" + entry["q_id"]
    p_data = None
    if "p_data" in entry and "q_data" in entry:
        p_data = p + "_" + entry["p_data"]
        q_data = q + "_" + entry["q_data"]
        size_p = signals[p_data]["size"]

    symb_name = "symb_" + p_trans_id
    if (size == "'0"):
        power_size = "1"
    else:
        power_size = "2**("+ size + "+1)"
    prop.write("// Modeling outstanding request for " + name + "\n")
    prop.write("reg [" + power_size + "-1:0] " + name + "_outstanding_req_r;\n")
    if p_data:
        prop.write("reg [" + power_size + "-1:0]["+size_p+":0] " + name + "_outstanding_req_data_r;\n")
    prop.write("\n")
    prop.write("always_ff @(" + clk_edge + " " + clk_sig + ") begin\n")
    prop.write("\tif(" + get_reset() + ") begin\n")
    prop.write("\t\t" + name + "_outstanding_req_r <= '0;\n")
    prop.write("\tend else begin\n")
    prop.write("\t\tif (" + p + "_hsk) begin\n")
    if size == "'0":
        prop.write("\t\t\t" + name + "_outstanding_req_r <= 1'b1;\n")
    else:
        prop.write("\t\t\t" + name + "_outstanding_req_r[" + p_trans_id + "] <= 1'b1;\n")
    if p_data:
        if size == "'0":
            prop.write("\t\t\t" + name + "_outstanding_req_data_r <= "+p_data+";\n")
        else:
            prop.write("\t\t\t" + name + "_outstanding_req_data_r[" + p_trans_id + "] <= "+p_data+";\n")
    prop.write("\t\tend\n")
    prop.write("\t\tif (" + q + "_hsk) begin\n")
    if size == "'0":
        prop.write("\t\t\t" + name + "_outstanding_req_r <= 1'b0;\n")
    else:
        prop.write("\t\t\t" + name + "_outstanding_req_r[" + q_trans_id + "] <= 1'b0;\n")
    prop.write("\t\tend\n")
    prop.write("\tend\n")
    prop.write("end\n")
    prop.write("\n")
    if "active" in entry:
        write_assert(prop, name + "_active", "|" + name + "_outstanding_req_r |-> "+entry["active"])
        prop.write("\n")
    else:
        prop.write("\n")

    prop.write("generate\n")
    prop.write("if (ASSERT_INPUTS) begin : " + name+ "_gen\n")
    if size == "'0":
        write_assert(prop, name + "1", "!" + name + "_outstanding_req_r |-> !(" + q + "_hsk)", indent="\t")
    else:
        write_assert(
            prop,
            name + "1",
            "!" + name + "_outstanding_req_r[" + symb_name + "] |-> !(" + q + "_hsk && (" + q_trans_id + " == " + symb_name + "))",
            indent="\t",
        )

    if size == "'0":
        expr2 = name + "_outstanding_req_r |-> s_eventually(" + q + "_hsk"
    else:
        expr2 = name + "_outstanding_req_r[" + symb_name + "] |-> s_eventually(" + q + "_hsk && (" + q_trans_id + " == " + symb_name + ")"
    if p_data:
        if size == "'0":
            expr2 += "&& (" + q_data + " == " + name + "_outstanding_req_data_r) )"
        else:
            expr2 += "&& (" + q_data + " == " + name + "_outstanding_req_data_r[" + symb_name + "]) )"
    else:
        expr2 += ")"
    write_assert(prop, name + "2", expr2, indent="\t")
    prop.write("end else begin : " + name+ "_else_gen\n")

    if p != handshakes[p]:
        write_assume(prop, name + "_fairness", p + "_val |-> s_eventually(" + p + "_rdy)", indent="\t")
    prop.write("\tfor ( j = 0; j < " + power_size + "; j = j + 1) begin : " + name+ "_for_gen\n")
    prop.write("\t\tco__" + name + ": cover property (" + name + "_outstanding_req_r[j]);\n")
    if size == "'0":
        write_assume(prop, name + "1", "!" + name + "_outstanding_req_r[j] |-> !(" + q + "_val)", indent="\t\t")
    else:
        write_assume(prop, name + "1", "!" + name + "_outstanding_req_r[j] |-> !(" + q + "_val && (" + q_trans_id + " == j))", indent="\t\t")

    if size == "'0":
        assume2 = name + "_outstanding_req_r[j] |-> s_eventually(" + q + "_val"
    else:
        assume2 = name + "_outstanding_req_r[j] |-> s_eventually(" + q + "_val && (" + q_trans_id + " == j)"
    if p_data:
        assume2 += "&& (" + q_data + " == " + name + "_outstanding_req_data_r[j]) )"
    else:
        assume2 += ")"
    write_assume(prop, name + "2", assume2, indent="\t\t")
    prop.write("\tend\n")
    prop.write("end\n")
    prop.write("endgenerate\n")
    prop.write("\n")

def gen_unique(prop, name, entry, p_key, q_key):
    p = entry["p"]
    q = entry["q"]
    p_trans_id = p + "_" + p_key[0]
    q_trans_id = q + "_" + q_key[0]
    size = p_key[1]
    symb_name = "symb_" + p_trans_id
    power_size = "2**("+ size + "+1)"
    prop.write("// Max 1 outstanding request for " + name + "\n")
    prop.write("reg [" + power_size + "-1:0] " + name + "_unique_outstanding_req_r;\n")
    prop.write("wire " + name + "_equal = " + p_trans_id + " == " + q_trans_id + ";\n")
    prop.write("\n")
    prop.write("always_ff @(" + clk_edge + " " + clk_sig + ") begin\n")
    prop.write("\tif(" + get_reset() + ") begin\n")
    prop.write("\t\t" + name + "_unique_outstanding_req_r <= '0;\n")
    prop.write("\tend else begin\n")
    prop.write("\t\tif (" + p + "_hsk) begin\n")

    prop.write("\t\t\t" + name + "_unique_outstanding_req_r[" + p_trans_id + "] <= 1'b1;\n")
    prop.write("\t\tend\n")
    prop.write("\t\tif (" + q + "_hsk) begin\n")
    prop.write("\t\t\t" + name + "_unique_outstanding_req_r[" + q_trans_id + "] <= 1'b0;\n")
    prop.write("\t\tend\n")
    prop.write("\tend\n")
    prop.write("end\n")
    prop.write("\n")

    prop.write("generate\n")
    prop.write("if (ASSERT_INPUTS) begin : " + name+ "_gen\n")
    write_assert(
        prop,
        name + "_unique",
        name + "_unique_outstanding_req_r[" + symb_name + "] |-> !(" + p + "_hsk && (" + p_trans_id + " == " + symb_name + "))",
        indent="\t",
    )
    prop.write("end else begin : " + name+ "_else_gen\n")
    prop.write("\tfor ( j = 0; j < " + power_size + "; j = j + 1) begin : " + name+ "_for_gen\n")
    write_assume(
        prop,
        name + "_unique",
        name + "_unique_outstanding_req_r[j] |-> !(" + p + "_val && (" + p_trans_id + " == j))",
        indent="\t\t",
    )
    prop.write("\tend\n")
    prop.write("end\n")
    prop.write("endgenerate\n")
    prop.write("\n")

    write_assert(
        prop,
        name + "_unique1",
        name + "_unique_outstanding_req_r[" + symb_name + "] |-> s_eventually(" + q + "_hsk && (" + q_trans_id + " == " + symb_name + "))",
    )
    prop.write("\n")

def link_submodules(submodule_assert,submodule_assume):
    files = []
    # Add binding of submodules
    base = os.getcwd()+"/ft_"
    basev = "${SVAPSHOT_ROOT}/ft_"
    for sub_name in submodule_assume:
        file_prop = sub_name+"/sva/"+sub_name+"_prop.sv"
        file_bind = sub_name+"/sva/"+sub_name+"_bind.svh"
        if os.path.exists(base+file_prop) and os.path.exists(base+file_bind):
            files.append(basev+file_prop+"\n")
            files.append(basev+file_bind+"\n")
    for sub_name in submodule_assert:
        file_prop = sub_name+"/sva/"+sub_name+"_prop.sv"
        file_bind = sub_name+"/sva/"+sub_name+"_bind.svh"
        if os.path.exists(base+file_prop) and os.path.exists(base+file_bind):
            files.append(basev+file_prop+"\n")
            new_bind = dut_name+"/sva/"+sub_name+"_bind.svh"
            files.append(basev+new_bind+"\n")
            # Write the new binding file from the old one
            old_bind_file = open(base+file_bind, "r")
            new_bind_file = open(base+new_bind, "w+")
            for line in old_bind_file:
                line = line.replace("INPUTS (0)", "INPUTS (1)")
                new_bind_file.write(line)
            old_bind_file.close()
            new_bind_file.close()
    return files

#_SKIP_LIB_SUBDIRS retired: library-walk policy lives in scaffold.tool_scripts


def gen_tcl(dut_root,ft_path, dut_folder, src_list, include, dut_name, dut_name_ext, submodule_assert, submodule_assume, module_name, module_type='sequential'):
    global clk_sig, rst_sig
    sub_filename = ft_path + "manual_sub.vc"
    vc_filename = ft_path + "files.vc"
    tcl_filename = ft_path + "FPV.tcl"

    # Determine file extension and if it's Verilog
    dut_file_ext = dut_name_ext.split(".")[-1]
    is_verilog = (dut_file_ext == "v")

    if (not os.path.exists(tcl_filename) or override_tool_script):
        tcl = open(tcl_filename, "w+")
        tcl.write("# Set paths to DUT root and FT root (based on environment variables)\n")
        tcl.write("set SVAPSHOT_ROOT $env(SVAPSHOT_ROOT)\n")
        tcl.write("set DUT_ROOT $env(DUT_ROOT)\n\n")
        # Use the following if you want to set the paths in the tcl script
        # tcl.write("set DUT_ROOT "+dut_root+"\n")
        # tcl.write("set SVAPSHOT_ROOT "+ft_path+"..\n\n")
        tcl.write("# Analyze design under verification files (no edit)\n")
        tcl.write("set DUT_PATH ${DUT_ROOT}/"+dut_folder+"\n")
        if src_list:
            index = 0
            for src_folder in src_list:
                tcl.write("set SRC_PATH"+str(index)+" "+src_folder+"\n")
                index+=1
        if include: tcl.write("set INC_PATH "+include+"\n")
        tcl.write("set PROP_PATH ${SVAPSHOT_ROOT}/ft_"+dut_name+"/sva\n\n")
        tcl.write("set_elaborate_single_run_mode off\n")
        tcl.write("set_automatic_library_search on\n")
        tcl.write("set_analyze_libunboundsearch on\n")
        tcl.write("set_analyze_librescan on\n")
        tcl.write("# Analyze property files\n")
        tcl.write("analyze -clear\n")
        tcl.write("check_cov -init -type all -model all\n")
        tcl.write("analyze -sv12 -f ${SVAPSHOT_ROOT}/ft_"+dut_name+"/files.vc\n")

        # If top-level is Verilog, analyze it separately
        if is_verilog:
            tcl.write("analyze -v2k ${DUT_ROOT}/"+dut_folder+ dut_name+"."+dut_file_ext+"\n")

        tcl.write("# Elaborate design and properties\n")
        tcl.write("elaborate -top " + module_name + " -disable_auto_bbox -create_related_covers {witness precondition}\n")
        tcl.write("# Set up Clocks and Resets\n")

        # Handle clock and reset based on module type
        if module_type == 'combinational':
            tcl.write("clock -none\n")
            tcl.write("reset -none\n\n")
        else:  # sequential
            tcl.write("clock "+clk_sig+"\n")
            tcl.write("reset -expression ("+get_reset()+")\n\n")

        tcl.write("# Get design information to check general complexity\n")
        tcl.write("get_design_info\n\n")
        tcl.write("set_word_level_reduction on\n")
        tcl.write("set_prove_time_limit 5m\n\n")
        tcl.write("set_proofgrid_max_jobs 180\n")
        tcl.write("set_proofgrid_manager on\n")
        tcl.write("autoprove -all\n")
        tcl.write("# Report proof results\n")
        tcl.write("report\n")
        tcl.write("# Measure coverage\n")
        tcl.write("check_cov -measure -type {coi stimuli proof bound}\n")
        tcl.write("check_cov -report\n")
        tcl.close()

    if not os.path.exists(sub_filename):
        vc = open(sub_filename, "w+")
        vc.write("// Add here defines if needed\n +define+<MACRO>\n")
        vc.write("// Add here further dependencies not captured automatically\n")
        vc.write("//${INC_PATH}/pkg.sv\n")
        vc.write("//${SRC_PATH}/submodule.sv\n")
        vc.write("//${DUT_PATH}/submodule.sv\n")
        vc.write("\n// Or blackbox modules like\n")
        vc.write("//-bbox_m submodule\n")
        vc.write("\n// Or add Macros like\n")
        vc.write("//+define+XPROP=1\n")
        vc.close()

    if (not os.path.exists(vc_filename) or override_tool_script):
        vc = open(vc_filename, "w+")
        vc.write("+libext+.v\n")
        vc.write("+libext+.h\n")
        vc.write("+libext+.sv\n")
        vc.write("+libext+.tmp.v\n")
        vc.write("+librescan\n")

        vc.write("+incdir+${DUT_PATH}\n")
        vc.write("-y ${DUT_PATH}\n")
        if src_list:
            index = 0
            for src_folder in src_list:
                string = "SRC_PATH"+str(index)
                index+=1
                vc.write("+incdir+${"+string+"}\n")
                vc.write("-y ${"+string+"}\n")
                for root, subdirs, files in os.walk(src_folder):
                    # Mutate in place so os.walk does not descend into skipped trees.
                    subdirs[:] = [d for d in subdirs
                                  if not should_skip_lib_subdir(d)]
                    if (verbose):  print("--"+string+" = " + root)
                    vc.write("-y "+root+"\n")

        if include:
            vc.write("+incdir+${INC_PATH}\n")
            vc.write("-y ${INC_PATH}\n")
        vc.write("-f ${PROP_PATH}/../manual_sub.vc\n")
        vc.write("${PROP_PATH}/"+ dut_name + "_prop.sv\n")
        vc.write("${PROP_PATH}/"+ dut_name + "_bind.svh\n")

        for line in link_submodules(submodule_assert,submodule_assume):
            vc.write(line)

        # Only add DUT file to files.vc if it's SystemVerilog
        if not is_verilog:
            vc.write("${DUT_ROOT}/"+dut_folder+ dut_name+"."+dut_file_ext+"\n")

        vc.close()

def gen_sby(dut_root, ft_path, dut_folder, src_list, include, dut_name, dut_name_ext, submodule_assert, submodule_assume):
    global clk_sig, rst_sig
    sby_filename = ft_path + "FPV.sby"
    sva_path = ft_path+"sva"

    if (not os.path.exists(sby_filename)):
        sby = open(sby_filename, "w+")
        sby.write("[tasks]\n")
        sby.write("cvr\n")
        sby.write("prv\n")
        #sby.write("live\n\n")
        sby.write("[options]\n")
        sby.write("cvr: mode cover\n")
        sby.write("prv: mode prove\n")
        #sby.write("live: mode live\n")
        #sby.write("depth 100\n\n")
        sby.write("[engines]\n")
        sby.write("cvr: smtbmc z3\n")
        sby.write("prv: abc pdr\n")
        #sby.write("live: aiger suprove\n\n")
        sby.write("[script]\n")
        sby.write("read -verific\n");
        # Link submodule property and bind files
        link_files = link_submodules(submodule_assert,submodule_assume)
        for line in link_files:
            strings = line.split("/")
            sby.write("read -sv "+strings[len(strings)-1])
        # Add other file dependencies
        if not recursive:
            # Only explicitely add the DUT top when it will not be added in the recursive search
            dut_file_ext = dut_name_ext.split(".")[-1]
            dut_full_name = dut_name+"."+dut_file_ext
            sby.write("read -sv "+dut_full_name+"\n");
        else:
            file_list = []
            read_list = []
            if src_list: src_list.append(dut_folder)
            else: src_list = [dut_folder]
            if include:
                src_list.insert(0, include)
            for src_folder in src_list:
                abs_path = dut_root+"/"+src_folder
                len_dut_root = len(dut_root)
                print(abs_path)
                for root, subdirs, files in os.walk(abs_path):
                    rel_path = "$DUT_ROOT"+root[len_dut_root:]
                    if (verbose): print("--"+src_folder+" = " + rel_path)
                    for name in files:
                        if name.endswith(".sv") or name.endswith(".v"):
                            file_list.append(rel_path+"/"+name)
                            read_list.append(name)
            for filename in read_list: sby.write("read -sv "+filename+"\n")

        sby.write("read -sv "+dut_name+"_prop.sv\n")
        sby.write("read -sv "+dut_name+"_bind.svh\n")
        sby.write("prep -top "+dut_name+"\n\n");
        sby.write("[files]\n")

        for line in link_files:
            sby.write(line)
        sby.write("$SVAPSHOT_ROOT/ft_"+dut_name+"/sva/"+ dut_name + "_prop.sv\n")
        sby.write("$SVAPSHOT_ROOT/ft_"+dut_name+"/sva/"+ dut_name + "_bind.svh\n")
        if not recursive:
            # No need to explicitely add the DUT top since it will be added in the recursive search
            sby.write("$DUT_ROOT/"+dut_folder+dut_full_name+"\n");
        else:
            for filename in file_list: sby.write(filename+"\n")
        sby.close()

def parse_former_prop(prop, prop_backup):
    former_sva = []
    save = False
    for orig_line in prop:
        prop_backup.write(orig_line)
        if (not save):
            line = clean(orig_line)
            if line.startswith("//====DESIGNER-ADDED-SVA====//"):
                save = True
        else:
            former_sva.append(orig_line)
            if (verbose):  print("Saving former designer SVA: " + orig_line)
    return former_sva




def reset_scaffold_state():
    """Clear module-level parse state so consecutive runs do not leak."""
    global params, interfaces, def_wires, assign_wires, fields, handshakes
    global implications, signals, suffixes, verbose
    global clk_sig, clk_edge, rst_sig, clk_found, rst_found, rst_active_low
    global prop_filename_backup, override_tool_script, recursive
    params = []
    interfaces = []
    def_wires = []
    assign_wires = {}
    fields = {}
    handshakes = {}
    implications = {}
    signals = {}
    suffixes = {}
    verbose = 0
    clk_sig = "clk"
    clk_edge = "posedge"
    rst_sig = "rst_n"
    clk_found = False
    rst_found = False
    rst_active_low = None
    prop_filename_backup = ""
    override_tool_script = 1
    recursive = 0


def scaffold_harness(
    filename,
    sources=None,
    include=None,
    module_type='sequential',
    tool='jasper',
    xprop_macro=None,
    submodule_assert=None,
    submodule_assume=None,
    dut_root=None,
    output_root=None,
    verbose_flag=False,
    work_dir=None,
):
    """Build ``ft_<module>/`` for *filename* (relative to *dut_root*).

    Parameters mirror the historical scaffold CLI. *output_root* is the tree
    that owns ``ft_*`` paths in generated TCL/``files.vc`` (``SVAPSHOT_ROOT``). *work_dir* is where ``ft_<module>/`` is created; it
    defaults to the process cwd, matching the old CWD-relative contract.
    """
    reset_scaffold_state()
    global verbose, prop_filename_backup

    former_sva = ["endmodule"]
    verbose = bool(verbose_flag)
    src_list = list(sources or [])
    submodule_assert = list(submodule_assert or [])
    submodule_assume = list(submodule_assume or [])
    dut_folder = filename
    xprop = xprop_macro
    if tool is None:
        tool = 'jasper'

    dut_root = dut_root or os.getenv('DUT_ROOT')
    if not dut_root:
        raise ScaffoldError('You must define DUT_ROOT')
    svapshot_root = (
        output_root
        or os.getenv('SVAPSHOT_ROOT')
    )
    if not svapshot_root:
        raise ScaffoldError(
            'You must define SVAPSHOT_ROOT')

    cwd = os.getcwd()
    if work_dir:
        os.chdir(work_dir)
    try:
        _scaffold_into(
            dut_root=dut_root,
            svapshot_root=svapshot_root,
            dut_folder=dut_folder,
            src_list=src_list,
            include=include,
            submodule_assert=submodule_assert,
            submodule_assume=submodule_assume,
            xprop=xprop,
            tool=tool,
            module_type=module_type,
            former_sva=former_sva,
        )
    finally:
        if work_dir:
            os.chdir(cwd)


def _scaffold_into(
    dut_root, svapshot_root, dut_folder, src_list, include,
    submodule_assert, submodule_assume, xprop, tool, module_type, former_sva,
):
    global verbose, prop_filename_backup

    dut_path = os.path.abspath(os.path.join(dut_root, dut_folder))
    dut_name = os.path.basename(dut_path).split(".")[0]
    ft_path = os.path.join(os.getcwd(), "ft_" + dut_name) + "/"
    create_dir(ft_path)
    sva_path = ft_path + "sva/"
    create_dir(sva_path)
    dut_folder_dir = dut_folder.split(".")[0]
    dut_folder_dir = dut_folder_dir[0:-len(dut_name)]

    prop_filename = sva_path + dut_name + "_prop.sv"
    prop_filename_backup = sva_path + dut_name + "_prop_old.sv"
    bind_filename = sva_path + dut_name + "_bind.svh"
    if os.path.exists(prop_filename):
        prop_old = open(prop_filename, "r+")
        prop_backup = open(prop_filename_backup, "w+")
        former_sva = parse_former_prop(prop_old, prop_backup)
        prop_old.close()
        prop_backup.close()

    rtl = open(dut_path, "r+")
    prop = open(prop_filename, "w+")
    bind = open(bind_filename, "w+")
    try:
        module_name = parse_global(rtl, prop, bind)
        adopt_body_clocking(dut_path)
        gen_vars(tool, prop, module_type)
        gen_models(prop)

        for key in assign_wires:
            prop.write("assign " + key + " = " + assign_wires[key] + "\n")
        if xprop:
            prop.write("\n//X PROPAGATION ASSERTIONS\n")
            if xprop != "NONE":
                prop.write("`ifdef " + xprop + "\n")
            for iface_name in suffixes:
                valid_name = iface_name + "_val"
                write_assert(prop, "no_x_" + valid_name,
                             "!$isunknown(" + valid_name + ")", indent="\t")
                for suffix in suffixes[iface_name]:
                    signal_name = iface_name + "_" + suffix
                    write_assert(
                        prop, "no_x_" + signal_name,
                        valid_name + " |-> !$isunknown(" + signal_name + ")",
                        indent="\t")
            if xprop != "NONE":
                prop.write("`endif\n")

        prop.write("\n//====DESIGNER-ADDED-SVA====//\n")
        for line in former_sva:
            prop.write(line)
    finally:
        rtl.close()
        if not prop.closed:
            prop.close()
        if not bind.closed:
            bind.close()

    if tool == "sby":
        gen_sby(dut_root, ft_path, dut_folder_dir, src_list, include,
                dut_name, dut_path, submodule_assert, submodule_assume)
    else:
        gen_tcl(dut_root, ft_path, dut_folder_dir, src_list, include,
                dut_name, dut_path, submodule_assert, submodule_assume,
                module_name, module_type)


def scaffold_main():
    """CLI entry compatible with the historical scaffold argument parser."""
    args = parse_args()
    try:
        scaffold_harness(
            filename=args.filename,
            sources=args.source,
            include=args.include,
            module_type=args.module_type,
            tool=args.tool or 'jasper',
            xprop_macro=args.xprop_macro,
            submodule_assert=args.submodule_assert,
            submodule_assume=args.submodule_assume,
            verbose_flag=args.verbose,
        )
    except ScaffoldError as exc:
        print(exc.message)
        sys.exit(exc.returncode)


def main_cli(argv=None):
    if argv is not None:
        sys.argv = [sys.argv[0]] + list(argv)
    scaffold_main()


if __name__ == '__main__':
    scaffold_main()
