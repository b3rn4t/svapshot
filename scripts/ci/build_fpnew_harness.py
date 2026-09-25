#!/usr/bin/env python3
"""Build a SVApshot ft_fpnew_top harness (no LLM) for CI formal smoke.

Writes under --workdir (typically ci_artifacts/formal_smoke):
  ft_fpnew_top/FPV_vcf.tcl and the rest of the scaffold/SVApshot tree.

Does not run VC Formal; the shell stage invokes run_vcf_batch.sh afterward.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--workdir',
        required=True,
        help='Directory that becomes SVAPSHOT_ROOT (ft_fpnew_top lands here)',
    )
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[2]
    rtl = (
        repo
        / 'benchmarks'
        / 'sargantana'
        / 'rtl'
        / 'datapath'
        / 'rtl'
        / 'exe_stage'
        / 'rtl'
        / 'fpu'
        / 'src'
        / 'fpnew_top.sv'
    )
    if not rtl.is_file():
        print(f'error: missing RTL: {rtl}', file=sys.stderr)
        return 2

    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    core = repo / 'src' / 'core'
    analysis = repo / 'src' / 'analysis'
    for path in (repo, core, analysis):
        p = str(path)
        if p not in sys.path:
            sys.path.insert(0, p)

    import main as svapshot_main  # noqa: E402

    sargantana = repo / 'benchmarks' / 'sargantana'
    rtl_root = sargantana / 'rtl'
    includes = str(rtl_root / 'includes')
    sources = [str(rtl_root)]

    os.chdir(workdir)
    os.environ['SVAPSHOT_ROOT'] = str(workdir)
    os.environ['DUT_ROOT'] = str(sargantana)

    rtl_path = str(rtl.resolve())
    if not svapshot_main.run_scaffold(
            rtl_path, sources, includes, 'sequential'):
        print('error: run_scaffold failed for fpnew_top', file=sys.stderr)
        return 1
    if not svapshot_main.validate_and_fix_property_bind_files(rtl_path):
        print('error: property/bind validation failed', file=sys.stderr)
        return 1
    if not svapshot_main.enhanced_create_manual_sub_and_inject_packages(
            rtl_path, sources, includes):
        print('error: package injection failed', file=sys.stderr)
        return 1
    if not svapshot_main.generate_fpv_vcf_tcl(
            rtl_path,
            'sequential',
            dut_root_override=str(rtl.parent.resolve())):
        print('error: generate_fpv_vcf_tcl failed', file=sys.stderr)
        return 1

    tcl = workdir / 'ft_fpnew_top' / 'FPV_vcf.tcl'
    if not tcl.is_file():
        print(f'error: expected {tcl}', file=sys.stderr)
        return 1
    print(f'OK: wrote {tcl}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
