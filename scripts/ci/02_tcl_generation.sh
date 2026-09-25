#!/usr/bin/env bash
# Stage 2: FPV TCL / harness generation smoke (SV + Verilog-95 reading).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ci_cd_repo
ci_set_status OK
ci_need_cmd python3 || { ci_set_status FAIL; printf 'error: python3 required\n' >&2; exit 1; }
ci_ensure_python_path

ci_log "TCL / Verilog harness smoke"
ci_run_unittest test_main test_harness test_scaffold test_verilog_reading
