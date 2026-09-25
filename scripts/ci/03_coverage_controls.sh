#!/usr/bin/env bash
# Stage 3: coverage default / fast path / hierarchical opt-in in generated TCL.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ci_cd_repo
ci_set_status OK
ci_need_cmd python3 || { ci_set_status FAIL; printf 'error: python3 required\n' >&2; exit 1; }
ci_ensure_python_path

ci_log "coverage controls"
ci_run_unittest \
    test_main.TestGeneratedVcFormalScript.test_top_level_coverage_is_the_default \
    test_main.TestGeneratedVcFormalScript.test_the_fast_path_has_no_coverage_instrumentation \
    test_main.TestGeneratedVcFormalScript.test_hierarchical_coverage_is_explicitly_opt_in \
    test_main.TestGeneratedVcFormalScript.test_post_proof_coverage_is_guarded_as_one_unit
