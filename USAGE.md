# AI-SVA Orchestrated Execution Guide

This document explains how to use the orchestrated AI-SVA system that automatically generates and verifies SystemVerilog Assertions.

## Overview

The orchestrated system (`main.py`) executes the following steps automatically:

1. **Harness scaffold** - Generates formal testbench and directory structure
2. **Initial generation** - Prompts the LLM with RTL and the shared SVA rules
3. **Reset TB** - Establishes initial property set from generated assertions
4. **Agent** - Performs syntax correction, set extension, and semantic correction

## Prerequisites

Make sure you have the following environment variables set:
- `NVIDIA_API_KEY` - For NVIDIA models
- `OPENAI_API_KEY` - For OpenAI models (if using OpenAI models)
- `DUT_ROOT` - Path to your RTL design root directory
- `SVAPSHOT_ROOT` - Path to the AI-SVA root directory

## Usage

### Basic Command Structure

```bash
python3 src/core/main.py <rtl_module> <llm_model> <execution_type> [verbosity]
```

### Parameters

1. **`rtl_module`** - Path to your RTL source file (e.g., `modules/hl_mul_unit.sv`)
2. **`llm_model`** - LLM model identifier (e.g., `meta/llama-3.1-405b-instruct`)
3. **`module_type`** - *Deprecated and optional.* Detected from the RTL, per module, so
   a combinational submodule under a sequential parent is treated correctly. Existing
   command lines that still pass `sequential` or `combinational` keep working; the value
   is used only if the file holds no module the tool can read, and a value that
   contradicts the RTL is reported and overridden.
4. **`execution_type`** - Execution mode:
   - `golden` - Full execution including semantic correction
   - `validation` - Execution without semantic correction (faster)
5. **`verbosity`** - Logging level (optional; defaults to `low`):
   - `low` - STEP progress only (`.:` / `:.` in-place updates); full step trail on failure
   - `medium` - Previous fine-grained orchestrator trail
   - `high` - Higher detail
   - `debug` - Debug verbosity

   Package detection always prints **one summary line per package** at every level.

### Optional Parameters

- `--sources` / `-src` - Source directories (default: `sources`)
- `--includes` / `-i` - Include directory (default: `packages`)

## Examples

### Sequential Module (Full Golden Execution)
```bash
python3 src/core/main.py modules/hl_mul_unit.sv meta/llama-3.1-405b-instruct golden medium
```

### Combinational Module (Validation Mode)
```bash
python3 src/core/main.py modules/adder.sv meta/llama-3.1-405b-instruct validation high
```

### With Custom Source Directories
```bash
python3 src/core/main.py modules/cpu.sv meta/llama-3.1-405b-instruct golden medium --sources src1 src2 --includes inc
```

## Module Type Behavior

The type is read out of the RTL by `rtl_clocking.py`, which both `main.py` and
`scaffold.engine` use so that the stage deciding the type and the stage writing
the clocking block cannot disagree.

### How the clock is identified
1. **The module's own clocked blocks.** `always_ff @(posedge clk, negedge rstn)`
   names the clock, says the reset is asynchronous, and gives its polarity.
   Port names cannot: `clock_divider_counter` declares `clk`, `clk_div`,
   `clk_div_valid` and `clk_out`, and only one of them is the clock.
2. **The port list**, for modules that instantiate clocked children without
   containing a clocked block themselves (41 of the modules under Sargantana's
   execution stage). A conventional name wins, then declaration order.
3. **Neither**, in which case the module is combinational.

### Sequential Modules
- **Clock**: the signal the registers run on (e.g., `clock clk_i`)
- **Reset**: with the polarity the sensitivity list states (e.g., `reset -expression (!rstn_i)`)
- **Use Case**: State machines, registers, counters, pipelined logic

### Combinational Modules  
- **Clock**: `clock -none`
- **Reset**: `reset -none`
- **Use Case**: Pure combinational logic, arithmetic units, decoders, multiplexers

## Execution Types

### Golden Mode
- Complete execution pipeline
- Includes syntax correction, set extension, and semantic correction
- Longer execution time but higher quality results
- Recommended for final verification

### Validation Mode
- Faster execution
- Skips semantic correction stage
- Good for iterative development and testing
- Maintains syntax correction and set extension

## Output Structure

After successful execution, you'll find:
```
ft_<module_name>/
├── sva/
│   ├── base                    # Initial assertions from RTL + SVA rules
│   ├── <module>_prop.sv       # Generated property file
│   ├── <module>_bind.svh      # Binding file
│   └── property_*             # Iteration files
├── FPV.tcl                    # Jasper TCL script
├── FPV_vcf.tcl                # VC Formal script (vacuity, formal core, density)
├── files.vc                   # File list
└── manual_sub.vc              # Manual dependencies

<assertions_dir>/
├── snapshot_manifest.json     # The snapshot: RTL revision, proofs, assumptions
├── snapshot_metrics.json      # Every reported figure, with its definition
├── qualification.json         # Per-property five-way qualification
├── coi_diversity.json         # Cone-of-influence spread of the proved set
├── llm_cost.json              # Token accounting and EUR cost per stage
├── assumptions.json           # Proposed assumptions and their screening
└── <module>_assume.svh        # Accepted assumptions as a standalone artifact

vcf_projs/<module>/reports/
├── properties.rpt             # report_fv -verbose (status + vacuity goals)
├── formal_core*.rpt           # What each proof actually depended on
├── assertion_density.rpt      # Tool-reported checker coverage
├── constraints.rpt            # Conflicting constraints and deadends
└── coi.rpt                    # Per-property cone of influence
```

## Qualifying and Evaluating a Snapshot

A snapshot is only worth freezing if its properties are proved
*non-vacuously*, and only worth trusting if it detects change. Both are
measured after generation, with the tools below. Each one reuses the testbench
`main.py` built, so the RTL, clock, reset and elaboration settings are
identical to the run being evaluated.

### Reading the metrics

`snapshot_metrics.json` and the report printed at the end of a run give, in
order of importance: proof status and vacuity, assumption dependence,
cone-of-influence diversity, mutation detection, runtime and LLM cost, and
only then checker coverage. Three different numbers are called coverage and
they are kept apart:

| Figure | Meaning |
| --- | --- |
| `checker_coverage` | Design signals in the cone of the proved properties, computed from the RTL by `coi.py` |
| `property_density` | The same idea measured by VC Formal over registers (`report_assertion_density`) |
| `formal_core_coverage` | Registers the non-vacuous proofs actually needed (`report_formal_core`) |

The formal core is a subset of the cone of influence, so the third figure is
always the smallest. A wide cone with a small core means the assertions
mention much of the design while their proofs lean on very little of it.

### Mutation-validated regression sensitivity

```bash
# inspect the mutant population without spending formal time
python3 src/analysis/evaluate.py mutants --rtl benchmarks/sargantana/rtl/.../rr_arb_tree.sv

# full evaluation: contract, mutants, detection rate, escape analysis
python3 src/analysis/evaluate.py sensitivity --rtl benchmarks/sargantana/rtl/.../rr_arb_tree.sv \
    --max-mutants 18 --max-per-class 3 \
    --manifest ft_rr_arb_tree/sva/snapshot_manifest.json
```

Mutants are single-point faults spread over control, datapath, timing, reset,
interface and state-update behaviour. Three kinds of undetectable edit are
dropped before any tool runs, because they are equivalent to the original by
construction and would only depress the rate: faults in generate branches the
parameters switch off, condition changes that decide the same way at
elaboration, and blocking-for-non-blocking rewrites of a register that is never
read inside its own always block. Every survivor is explained, either as logic
outside every property cone or as logic inside one that no property constrains
precisely enough.

### Screening the generated assumptions

```bash
python3 src/analysis/evaluate.py assumptions --rtl benchmarks/sargantana/rtl/.../rr_arb_tree.sv
```

Measures the same property set with and against the identical mutants, with
the assumption block in force and with it stripped out. An assumption that
removes a mutant from the detected set has narrowed the input space far enough
to hide a real fault, and the command exits non-zero naming it.

### Checking a candidate revision

```bash
python3 src/analysis/evaluate.py regression --rtl rr_arb_tree.sv \
    --candidate rr_arb_tree_refactored.sv \
    --manifest ft_rr_arb_tree/sva/snapshot_manifest.json \
    --changed a_grant_vector_is_onehot0
```

Properties named in `--changed` are the behaviour the author meant to alter;
they are reported as needing re-specification. Anything else that breaks is an
unintended change and the command exits 3. This is how an AI-generated
refactor is judged without conflating the two.

### Comparing against other generation methods

```bash
python3 src/analysis/baselines.py compare --rtl benchmarks/sargantana/rtl/.../rr_arb_tree.sv \
    --methods svapshot,one_shot --llm gpt-4.1-mini

# bring a published artifact under the same harness
python3 src/analysis/baselines.py compare --rtl ... --methods imported \
    --import lasso=artifacts/lasso_rr_arb_tree.sv --import-cost lasso=0.42
```

Every method contributes only a property set; the checker shell, the mutants
and the qualification are shared, so the comparison is between the properties
and nothing else. The summary reports the LLM Efficiency Score,

    LES = proved_non_vacuous x mutation_detection_rate / max(cost, floor)

which a method cannot win by emitting many cheap, trivially provable
properties: a set that catches no injected fault scores zero.

### Unit tests

```bash
python3 -m unittest discover -s tests   # every suite, about three seconds
```

`tests/test_svapshot.py` covers every module that runs without a formal tool or
an LLM: the log parser and its qualification, cone extraction, the mutation
engine, adaptive stopping, cost accounting, and the metric definitions.

`tests/test_harness.py` covers the native harness-scaffold stage against
Sargantana's execution stage, driving `scaffold.scaffold_harness` in-process the
way `main.run_scaffold` does: the directory skeleton and its five artifacts, the
port mirror (including the 52 ports and the `` `ifdef ``-guarded ports of
`exe_stage`), clock and reset selection, the bind, `files.vc`, `FPV.tcl`,
preservation of designer-added SVA across regeneration, and the
`DUT_ROOT`/`SVAPSHOT_ROOT` guards.

`tests/test_scaffold.py` covers the small scaffold helpers: assertion-id
normalisation, display-string escaping, and the library-walk skip policy used
when writing `files.vc`.

`tests/test_main.py` covers harness construction: DUT-root and relative-path
resolution, clocking analysis and per-module classification, submodule
detection, package resolution, transitive closure and injection, include
discovery, expansion of `files.vc` into `files_vcf.vc`, the per-extension
language selection that lets a Verilog design and a SystemVerilog checker
compile in one run, and the contents of the generated `FPV_vcf.tcl`, including VCS `-pvalue`
when a parent constant was resolved for a leaf. Tests that
need an `ft_<module>/` tree build one via the native scaffolder.

`tests/test_instantiation_params.py` covers the parent-instantiation context
fed to leaf prompts: direct `#()` overrides only, shared literals vs differing
or parent-expression values, untouched module defaults, and inclusion of that
block in the initial-generation prompt. Shared parent expressions that fold
to compile-time literals (`Features.Width` → `64`) are also applied at
leaf FPV elaborate via VCS `-pvalue`, so illegal header-default combinations
do not crash elaboration. Based literals (`1'b1`, `5'b11111`) are rewritten
to decimal on that command line because VC Formal launches VCS with
`sh -c` and a raw `'` breaks the shell. Generate-index and function
expressions are left alone.

`tests/test_cex_context.py` covers the CEX-aware semantic-repair prompt: active
assumptions, vacuous-versus-falsified instructions, bound depth and engine in
words, formal-core / COI signals, sibling proofs, last-attempt solver progress,
and a compact cycle table parsed from a VCD or a text dump. Vacuous proofs get
no waveform. Isolated repair dumps `fvtrace` FSDB under
`vcf_projs/<module>/reports/cex/` after `check_fv`; the prompt prefers a
sibling `.vcd` / `.txt` when one is present, otherwise a depth-only skeleton.
The repair RTL dump is restricted to formal-core / property signals. Nothing
in that suite invokes `vcf`.

`tests/test_fpv_rtl_bench.py` covers portable FPV-RTLBench seed packages and
the parallel sample-eval planner: isolated `runs/` trees, TuRTLe discovery,
and stable (design, model) shards. It does not invoke VC Formal.

`tests/test_verilog_reading.py` covers reading a Verilog design, which is what
supporting both HDLs actually requires: the checker is SystemVerilog whatever the
DUT is written in, so the assertion side needs no change, while every stage that
reads the RTL does. Its 27 tests use the OpenCores FIFO family in
`benchmarks/2605.06434/fifo/` — Verilog-95 in shape, with bare port names in the
header, several modules per file and a port named `do` — and pin that each module
of a file is described by its own ports, that directions declared in the body are
read (an output mistaken for an input can be constrained by an environment
assumption, which assumes the conclusion), and that a `.v` design is parsed with
Verilog's keywords while assertion text is still parsed with SystemVerilog's.

`tests/test_assertion_template.py` treats model output as untrusted input. Its
77 tests cover the prompt contract, legal assertion identifiers, display-string
escaping, strict structured-field parsing, bounded fallback extraction of
legacy SVA, designer-section splitting, renaming, reduction of an assertion to
the property text the database is given, and idempotent conversion. Malformed
blocks become no source code; valid siblings still survive.

`tests/test_agent_preprocessing.py` covers the first deterministic task in
`agent.py`. Its 54 tests qualify only RTL-proven DUT internals, parameters and
enum labels inside the balanced property expression; protect ports, comments,
failure strings, hierarchy and package scopes; exercise real arrayed signals
from Sargantana's `div_unit`; guarantee collision-free assertion labels in both
preprocessing and the final property-file writer; and pin the handoff to the
assertion database, which passes the property alone and leaves the failure
action, the comments and the message behind.

`tests/test_svalint_wrapper.py` covers the lint gate before assertion database
construction. Its 54 tests invoke both a controlled temporary subprocess and
the installed `SVALint/` checkout. They exercise clean, warning, violation,
crash and timeout outcomes; confirm real SVALint's zero exit status on
violations and stderr output; verify explicit rule configuration; robustly
parse formatting variations; build targeted LLM feedback; and ensure missing
tools or failed runs close the gate without consuming a syntax-repair model
call. Installed-checkout cases skip when SVALint's documented Python and
Verible dependencies are unavailable.

`tests/test_svaparser.py` covers the assertion database itself, which decides
whether the model produced a new property and therefore when the loop stops. Its
114 tests establish logical faithfulness by truth table against a reference
boolean parser written independently in the suite; check that operands such as
`$past(...)`, comparisons, slices, concatenations and reductions survive as
single atoms; require refusal rather than approximation for what cannot be
converted; and pin duplicate detection over reordered conjuncts, redundant
parentheses and relabelled copies. Every case is a fixed string, so no model is
involved.

`tests/test_svaparser_corpus.py` runs the parser over real assertions instead of
chosen ones. `tests/collateral/assertion_corpus.sv` holds the 2641 distinct
assertion shapes taken from every property file SVApshot has written across
Sargantana, the PTW, the TLB, the FPU blocks and the I2C master; nothing else in
the suite reads a file, so the rest keeps working if the collateral is deleted and
these cases skip; `tests/collateral/build_assertion_corpus.py` rewrites it from
the tree when new property files are worth covering. A slice spread across the
collection runs on every invocation
and costs about a second: no assertion may raise, produce a placeholder or
stringified literal, lose an operand across an implication, or fail the
quarantine gate, and a batch replayed in reverse must add nothing. The full pass
also bounds how many assertions the parser refuses:

```bash
SVAPSHOT_FULL_CORPUS=1 python3 tests/test_svaparser_corpus.py  # about 40 s
```

`tests/test_agent_extension.py` covers the loop that asks the model for more
assertions until a batch adds none. Its 44 tests script the model — batches
prepared in advance, handed back through the same call the loop makes — so no key
or licence is involved and the suite runs in under a second. They pin the stop
condition and the restatements that must not restart it (reordered conjuncts, a
delay written out, a relabelled copy, a bitwise connective, De Morgan); that
duplicates never reach syntax correction and cost no model call; that the
property file and the assertion database end up holding the same properties; the
assertion cap, including that what it discards is not left in the database; what
the extension prompt carries and what is done with an unusable response; and
properties the parser cannot convert, which must still be counted exactly once.
Twelve assertions from the stored corpus drive the same loop, replayed relabelled
and in reverse.

The scaffold and main suites skip their Sargantana-dependent cases if the RTL
tree is missing. The agent preprocessing suite skips only its two
real-RTL cases when Sargantana is absent; its synthetic contracts and the
assertion-template, SVALint-wrapper, parser and pure-module suites need neither.
No suite needs a formal tool or an LLM. What each stage pins, and the
seventy-nine defects exposed so far, is written up in
`svapshot_component_tests_report.html`.

## Environment Setup

```bash
# Set required environment variables
export DUT_ROOT=/path/to/your/rtl/root
export SVAPSHOT_ROOT=/path/to/ai-sva
export NVIDIA_API_KEY=your_nvidia_api_key

# Run the orchestrated system
python3 src/core/main.py modules/your_module.sv meta/llama-3.1-405b-instruct golden medium
```

## Troubleshooting

### Common Issues

1. **Missing Environment Variables**
   ```
   Error: You must define DUT_ROOT
   ```
   Solution: Set `DUT_ROOT` and `SVAPSHOT_ROOT` environment variables

2. **Python Version Issues**
   - The full flow runs under Python 3 (including harness scaffolding)
   - Use the same interpreter for `main.py`, unit tests, and CHIA stages

3. **Module Type Selection**
   - Nothing to select: every module is classified from its own RTL
   - A `sequential`/`combinational` argument left on an old command line is
     reported and overridden when it contradicts the design

4. **API Key Issues**
   - Ensure your API key is valid and has sufficient credits
   - Check network connectivity for API calls

### Debug Mode

For detailed debugging, use `debug` verbosity:
```bash
python3 src/core/main.py modules/debug_module.sv meta/llama-3.1-405b-instruct golden debug
```

This will provide extensive logging throughout the execution pipeline.

## Available RTL Modules

The following RTL modules are available in the `modules/` directory:

- `hl_mul_unit.sv` - Hardware loop multiplier unit
- `hl_div_unit.sv` - Hardware loop division unit
- `hl_alu.sv` - Hardware loop ALU
- `on_supply_enabler.sv` - Supply enabler module
- `general_purpose_fifo.sv` - FIFO buffer
- `packing_buffer.sv` - Packing buffer
- `popcount.sv` - Population count
- And many more...

## LLM Models

Common LLM models you can use:

- `meta/llama-3.1-405b-instruct` - Meta Llama model
- `meta/llama-3.3-70b-instruct` - Smaller Llama model
- `nvdev/nvidia/nemotron-4-340b-instruct-128k` - NVIDIA Nemotron model
- `gpt-4-turbo-preview` - OpenAI GPT-4 (requires OPENAI_API_KEY)

## Results Analysis

After execution, you can analyze the results using:

1. **Verification UI**: Run `python3 -m svapshot_ui.server --workspace .` and open
   `http://127.0.0.1:8765/`; it reads `ft_<module_name>/sva/snapshot.json`
2. **Property Files**: Check the generated property files in `ft_<module_name>/sva/`
3. **Timing Report**: Review execution timing in the console output
4. **Interaction Logs**: Examine LLM interactions in the `interactions/` directory

The UI is read-only. It does not launch formal or LLM work, and it requires the
canonical snapshot artifact generated by the current agent.

For more detailed information about individual components, refer to the README.md file. 