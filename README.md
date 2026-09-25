# SVApshot

**Version 0.1**

LLM-driven SystemVerilog assertion (SVA) snapshots for RTL. A snapshot is a
set of properties that VC Formal (or JasperGold) proves non-vacuously.

This tree is the generator used by the loop at
[b3rn4t/chia-svapshot](https://github.com/b3rn4t/chia-svapshot). It does **not**
ship DUT RTL (`modules/`, `benchmarks/sargantana`) or a Synopsys install; those
live on the host.

| Path | Role |
| --- | --- |
| `src/core/` | Scaffold, LLM agent, repair, VC Formal Tcl |
| `src/analysis/` | Proof parsing, COI/diversity, mutation scoring |
| `fpv_app_scripts/` | Batch `vcf` / JasperGold launchers |
| `SVALint/` | Optional linter (submodule) |
| `USAGE.md` | `main.py` flags and flow |

## Host values you fill in

Replace every `<…>` (nothing in this repo is a working install path or key).

| Variable | Required when | Example shape |
| --- | --- | --- |
| `DUT_ROOT` | always (this checkout has no `modules/` tree) | `/path/to/your/rtl` |
| `VC_FORMAL_HOME` | proving with VC Formal | `/path/to/synopsys/vc_formal` |
| `SNPSLMD_LICENSE_FILE` | VC Formal licence | `27000@licence-host` or a `.lic` path |
| `NVIDIA_API_KEY` | NVIDIA / `meta/…` models | from build.nvidia.com |
| `OPENAI_API_KEY` | OpenAI models | from the OpenAI dashboard |
| `CURSOR_API_KEY` | `cursor:…` / Composer models | from Cursor |
| `GOOGLE_CLOUD_PROJECT` | `gemini-*` (Vertex) | GCP project id |
| `GOOGLE_CLOUD_LOCATION` | Vertex (optional) | `global` if unset |

JasperGold: put `jg` on `PATH` instead of setting `VC_FORMAL_HOME`.

## Setup

```bash
git clone --recurse-submodules git@github.com:b3rn4t/svapshot.git
cd svapshot
export SVAPSHOT_ROOT="$PWD"
export DUT_ROOT=/path/to/your/rtl          # host RTL tree (not in this repo)

export VC_FORMAL_HOME=/path/to/vc_formal
export SNPSLMD_LICENSE_FILE=27000@licence-host
# export PATH="$VC_FORMAL_HOME/bin:$PATH"  # setup script does this when the home is set

# One LLM backend, matching the model you pass to main.py:
export NVIDIA_API_KEY=                       # or OPENAI_API_KEY / CURSOR_API_KEY
# export GOOGLE_CLOUD_PROJECT=               # Vertex / gemini-* only

source scripts/setup_svapshot.sh             # exports roots; does not store keys
```

`scripts/setup_svapshot.sh` defaults `DUT_ROOT` to `$SVAPSHOT_ROOT/modules`,
which is **not** shipped. Always export `DUT_ROOT` yourself.

## Run (`main.py`)

One module, one model. **`golden` is the intended flow**: scaffold, LLM seed,
syntax repair, set extension, and semantic correction (RTL context, up to five
attempts). `validation` skips that last stage (one spec-based attempt) and is
only a cheaper smoke path.

```bash
python3 src/core/main.py path/inside/dut/module.sv meta/llama-3.3-70b-instruct golden \
  --formal-tool vcformal
```

Positionals (clocking is **not** a positional anymore; it is read from the RTL):

| Argument | Meaning |
| --- | --- |
| `rtl_module` | Path to the leaf `.sv` / `.v`, relative to `DUT_ROOT` |
| `llm_model` | Catalog name: NVIDIA `meta/…`, OpenAI `gpt-…`, Vertex `gemini-…`, or `cursor:…` |
| `module_type` | Optional, deprecated. `auto` (default) / `sequential` / `combinational`. Detection from the file wins unless it cannot classify |
| `execution_type` | **`golden`** (full) or `validation` (no semantic-correction loop) |
| `verbosity` | Optional; default `low`. `medium` / `high` / `debug` restore the orchestrator trail |

Useful flags:

| Flag | Default | Use |
| --- | --- | --- |
| `--formal-tool` / `-ft` | `jaspergold` | `vcformal` or `jaspergold` |
| `--sources` / `-src` | `sources` | Extra RTL search dirs (repeatable) |
| `--includes` / `-i` | `packages` | Package / include dir for the filelist |
| `--assertion-source` / `-as` | `rtl` | `rtl` = LLM from the design + SVA rules; `file` = seed from `--initial-file` |
| `--initial-file` / `-if` | `initial_assertions` | Seed SVA when `-as file` |
| `--parent-rtl` | (empty) | Parent that instantiates this leaf; used only for `#()` / `-pvalue`, not as the formal top |
| `--verilog-std` | `2001` | How `.v` sources are compiled (`1995` if a 2001 keyword is an identifier) |
| `--disable-package-detection` / `-dpd` | off | Do not auto-fill `manual_sub.vc` packages |
| `--stop-after-scaffold` | off | Write `ft_<module>/` + `FPV_vcf.tcl`, then exit |
| `--stop-after-extension` | off | Stop after set extension (before semantic correction) |
| `--resume-from-extension` | off | Reuse the after-syntax set; continue at extension |
| `--resume-from-semantic` | off | Reuse the harness; continue at semantic correction |

`--resume-from-semantic` and `--resume-from-extension` cannot be combined;
`--stop-after-scaffold` cannot mix with the resume/extension flags.

Worked variants:

```bash
# Seed from a file instead of prompting the LLM on the RTL
python3 src/core/main.py path/inside/dut/module.sv meta/llama-3.3-70b-instruct golden \
  --formal-tool vcformal --assertion-source file --initial-file seeds/module.sva

# Leaf whose parameters come from a parent instantiation
python3 src/core/main.py rtl/leaf.sv meta/llama-3.3-70b-instruct golden \
  --formal-tool vcformal --parent-rtl rtl/parent.sv
```

Output is `ft_<module>/` (SVA, bind, Tcl) plus `vcf_projs/<module>/` or the
JasperGold project, depending on `-ft`. More narrative: `USAGE.md`.

## Other execution scripts

`main.py` is one DUT × one model. The wrappers below call it (or the same Tcl)
in a loop.

**`src/core/multi_llm_experiment.py`** — same RTL through three models, then
rename `ft_*` folders so results do not overwrite each other. Optional JasperGold
coverage (`-rc`) on property snapshots `i/s/e/c/f` (initial, after syntax,
extension, semantic correction, final).

```bash
python3 src/core/multi_llm_experiment.py path/inside/dut/module.sv sequential golden medium \
  --llm-models meta/llama-3.3-70b-instruct gpt-4.1-mini meta/llama-3.1-405b-instruct \
  --experiment-name my_run
```

Omitted positionals are prompted. Pass `--keep-intermediate` to retain per-model
scratch. Needs the same env as `main.py` (and `OPENAI_API_KEY` if a GPT model is
in the triple).

**`src/core/publication_evaluation_experiment.py`** — YAML matrix (controlled /
ablation / sensitivity tracks) via `PublicationRunner`. Needs
`experiments/svapshot_evaluation.yaml`, which this 0.1 tree does not ship.
`--list` / `--dry-run` / `--execute`, with `--track`, `--design`, `--method`,
`--model`, `--seed`, `--limit`.

**`fpv_app_scripts/`** — prove an already-scaffolded `ft_<DUT>/` without
re-running the LLM:

| Script | Tool |
| --- | --- |
| `run_vcf_batch.sh <module>` | VC Formal batch (`FPV_vcf.tcl` → `vcf_projs/<module>/vcf.log`) |
| `run_vcf.sh` / `run_jg.sh` / `run_jg_batch.sh` | Interactive or batch VC Formal / JasperGold |
| `run_sby.sh <module>` | SymbiYosys, if that flow is on `PATH` |

`run_vcf_batch.sh` requires `VC_FORMAL_HOME`. Set `SVAPSHOT_VCF_DOCKER` if `vcf`
must run inside a named container.

**`scripts/run_vcformal.sh`** — start the optional `vcformal` Docker image with
this checkout bind-mounted (UID-preserving). Not required if `vcf` is already
on the host `PATH`.

The CHIA loop sets `SVAPSHOT_ROOT` to this checkout when it is the `svapshot`
submodule.
