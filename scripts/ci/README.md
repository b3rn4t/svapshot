# SVApshot CI sanity (local / future GitLab)

Reusable entrypoints for the six MR test-plan checks. Same scripts run on a
laptop, inside the `vcformal` container, or later from `.gitlab-ci.yml`.

## Inside an already-running `vcformal` container

From the repository root:

```bash
./scripts/ci/sanity.sh --require-formal
```

That runs all six stages. Soft-skip is off for formal, so stage 4 builds
`ft_fpnew_top` via SVApshot and runs `run_vcf_batch.sh fpnew_top`.
Stage 1 always sets `SVAPSHOT_FULL_CORPUS=1` (and GitLab also sets
`SVAPSHOT_REQUIRE_FULL_CORPUS=1`) so the assertion corpus is never sliced.
Stage 6 walks the same corpus through `SVAParser.convert_to_cnf`.

```bash
./scripts/ci/sanity.sh                          # may SKIP 4/5 if tools missing
./scripts/ci/sanity.sh --only 4 --require-formal
./scripts/ci/sanity.sh --skip 5
```

Do **not** source `scripts/setup_svapshot.sh` here (it contains secrets).

## From the host (new container)

```bash
./scripts/ci/run_in_docker.sh
CI_IMAGE=vcformal:latest ./scripts/ci/run_in_docker.sh --require-formal
```

Default image is `python:3.11-bookworm` (stages 1–3; 4–5 soft-SKIP without `vcf`).
`run_in_docker.sh` always passes `--user $(id -u):$(id -g)` so bind-mounted
writes keep host ownership.

Interactive local work uses `./scripts/run_vcformal.sh` (or the `vcformal`
shell function, which prefers that script). Rebuild the image with
`./build/build.sh` so the baked-in UID matches the host; the launcher still
overrides with `--user` even on an older root-default image.

## Running on `rl9-snps-vc` (GitLab CI)

That image already provides VC Formal (`VC_STATIC_HOME` / `vcf` on `PATH`).
It does **not** provide SVApshot's Python stack or the SVALint
tooling. A reference job that installs everything around the image is in
[`.gitlab-ci.yml`](../../.gitlab-ci.yml) at the repo root — show that to
sysadmins. Condensed form:

```bash
# non-pip: verible-verilog-syntax; submodules via scripts/ci/update_submodules.sh
pip3 install --no-cache-dir -r build/requirements-ci.txt
source scripts/ci/update_submodules.sh
export PYTHONPATH="$PWD/src/core:$PWD/src/analysis:$PWD"
export SVALINT_ROOT="$PWD/SVALint"
./scripts/ci/sanity.sh --require-formal
```

Or set `CI_PIP_INSTALL=1` and let `sanity.sh` / `lib.sh` install
`build/requirements-ci.txt` itself.

### Python (`build/requirements-ci.txt`)

Minimal surface for `sanity.sh` only:

- `antlr4-python3-runtime` — assertion database / structural parser
- `anytree`, `tomli` — SVALint Python bridge

No LLM provider (`openai`), no `tiktoken`, no `matplotlib`/`numpy`. Full tool
runs still use `build/requirements.txt`.

### Outside pip (must be on the image or installed in the job)

| Need | Why | On `rl9-snps-vc`? |
|---|---|---|
| `python3` + `pip3` | tool + unit tests + harness scaffold | usually yes (confirm) |
| `vcf` + license (`SNPSLMD_LICENSE_FILE`) | formal smoke | yes (that is the image) |
| `verible-verilog-syntax` | SVALint gate | **no** — install the Verible static binary |
| `SVALint/` tree | lint rules; pointed at by `SVALINT_ROOT` | **submodule** — `git submodule update --init SVALint` |
| `benchmarks/sargantana` | stage 4 `fpnew_top` RTL plus nested csr/mmu/cvfpu | submodule — rewrite `../csr.git`/`../mmu.git` to GitHub, then recurse |
| `git`, `bash`, `timeout` | scripts / sanity | usually yes |

Cursor CLI, GitHub Copilot CLI, Node.js and the ANTLR *jar* are **not**
required for CI. Generated `sva_grammar/` parsers are committed; only the
Python ANTLR runtime is needed at run time.

### What will not work as-is on that image

1. **No Verible / SVALint** — lint tests soft-SKIP; production lint will not
   run until both are present.
2. **No SVApshot source baked in** — CI must check out this repo (and SVALint)
   and set `PYTHONPATH` / `SVALINT_ROOT`.
3. **No pip packages baked in** — always `pip install -r build/requirements-ci.txt`.
4. **Shard layout is fine for `vcf`** — the CI Dockerfile copies the split
   Synopsys trees back into place; batch FPV does not need the dropped `doc/`
   tree. Hector / unused SG paths are irrelevant to SVApshot's smoke.
5. **Ownership** — if the job bind-mounts a host workspace and the container
   runs as root, `ft_*` / `ci_artifacts/` become root-owned on the host. Run
   the job as the workspace UID (GitLab usually does) or pass
   `--user $(id -u):$(id -g)` when launching Docker yourself.

## Stages

| # | Script | What it checks |
|---|---|---|
| 1 | `01_unit_tests.sh` | `python3 -m unittest discover -s tests` (`SVAPSHOT_FULL_CORPUS=1`; CI refuses a slice) |
| 2 | `02_tcl_generation.sh` | `test_main`, `test_harness`, `test_verilog_reading` |
| 3 | `03_coverage_controls.sh` | Coverage default / fast path / hierarchical opt-in TCL tests |
| 4 | `04_formal_smoke.sh` | SVApshot `ft_fpnew_top` + `run_vcf_batch.sh` (coverage off) |
| 5 | `05_docker_packaging.sh` | Dockerfile/`build.sh` presence + `bash -n` |
| 6 | `06_sva_cnf_conversion.sh` | Full-corpus `convert_to_cnf` / `flatten_to_cnf`; fails on refusal / crash / `_cnf_imperfect` regressions |

Stages 1–3 use `run_unittest_visual.py` (per-module `.:` / `:.` progress, muted
test stdout unless a case fails). `sanity.sh` prints a titled summary and points
at `ci_artifacts/`.

## Future GitLab CI

The reference pipeline is [`.gitlab-ci.yml`](../../.gitlab-ci.yml). It uses
`rl9-snps-vc`, installs Verible / SVALint / `requirements-ci.txt`
in `before_script`, then runs:

```bash
./scripts/ci/sanity.sh --require-formal
```

Show that file to sysadmins as the contract for what the job expects around
their image.
