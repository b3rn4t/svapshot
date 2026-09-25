# Shared helpers for scripts/ci/*. Source from stage scripts and sanity.sh.
# Do not source scripts/setup_svapshot.sh (secrets).

set -euo pipefail

CI_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${CI_LIB_DIR}/../.." && pwd)"

# shellcheck disable=SC2034
CI_REQUIRE_FORMAL="${CI_REQUIRE_FORMAL:-0}"
CI_REQUIRE_IMAGE_BUILD="${CI_REQUIRE_IMAGE_BUILD:-0}"

ci_cd_repo() {
    cd "${REPO_ROOT}"
}

ci_need_cmd() {
    local cmd="$1"
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        return 1
    fi
    return 0
}

ci_log() {
    printf '==> %s\n' "$*"
}

ci_warn() {
    printf 'warning: %s\n' "$*" >&2
}

ci_ensure_python_path() {
    # tests/ must be importable for targeted unittest module names.
    export PYTHONPATH="${REPO_ROOT}/tests${PYTHONPATH:+:${PYTHONPATH}}"
}

ci_pip_install_if_needed() {
    # Install only when CI_PIP_INSTALL=1 (generic containers / rl9-snps-vc).
    # Local vcformal images already bake build/requirements.txt.
    if [[ "${CI_PIP_INSTALL:-0}" == "1" ]]; then
        local req="${REPO_ROOT}/build/requirements-ci.txt"
        if [[ ! -f "${req}" ]]; then
            req="${REPO_ROOT}/build/requirements.txt"
        fi
        ci_log "pip install -r ${req#"${REPO_ROOT}"/}"
        python3 -m pip install --quiet -r "${req}"
    fi
}

# Stages write OK|SKIP|FAIL into this file when CI_STATUS_FILE is set.
ci_set_status() {
    local status="$1"
    if [[ -n "${CI_STATUS_FILE:-}" ]]; then
        printf '%s\n' "${status}" >"${CI_STATUS_FILE}"
    fi
}

ci_skip() {
    # Soft skip: print reason and exit 0 unless a require-* flag is set.
    # Usage: ci_skip "reason" [require_flag_var]
    local reason="$1"
    local require_var="${2:-}"
    if [[ -n "${require_var}" && "${!require_var}" == "1" ]]; then
        ci_set_status FAIL
        printf 'error: required stage cannot skip: %s\n' "${reason}" >&2
        exit 1
    fi
    ci_set_status SKIP
    printf 'SKIP: %s\n' "${reason}"
    exit 0
}

ci_run_unittest() {
    # Visual unittest runner used by stages 1–3.
    python3 "${CI_LIB_DIR}/run_unittest_visual.py" "$@"
}
