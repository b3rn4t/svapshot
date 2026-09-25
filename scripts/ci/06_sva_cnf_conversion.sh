#!/usr/bin/env bash
# Stage 6: large SVA → CNF conversion over the full assertion corpus.
# Fails on converter crashes, refusal-count regressions, or _cnf_imperfect spikes.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ci_cd_repo
ci_set_status OK
ci_need_cmd python3 || { ci_set_status FAIL; printf 'error: python3 required\n' >&2; exit 1; }

CORPUS="${REPO_ROOT}/tests/collateral/assertion_corpus.sv"
if [[ ! -f "${CORPUS}" ]]; then
    printf 'error: assertion corpus missing: %s\n' "${CORPUS}" >&2
    exit 1
fi

OUT="${REPO_ROOT}/ci_artifacts/sva_cnf_conversion"
mkdir -p "${OUT}"

ci_log "SVA → CNF conversion over the full assertion corpus"
python3 "${SCRIPT_DIR}/run_sva_cnf_conversion.py" --output-dir "${OUT}"
