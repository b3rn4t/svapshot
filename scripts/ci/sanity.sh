#!/usr/bin/env bash
# Orchestrate the six SVApshot sanity stages (MR test plan).
#
# Usage (repo root, inside vcformal or any Python-capable env):
#   ./scripts/ci/sanity.sh
#   ./scripts/ci/sanity.sh --require-formal
#   ./scripts/ci/sanity.sh --only 4 --require-formal
#   ./scripts/ci/sanity.sh --skip 5
#
# Future GitLab CI should call this script with the same flags.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

# Sanity always exercises the full assertion corpus (not the default slice).
# Override with SVAPSHOT_FULL_CORPUS=0 if you need a faster local discover.
export SVAPSHOT_FULL_CORPUS="${SVAPSHOT_FULL_CORPUS:-1}"

ONLY=""
SKIP=""

usage() {
    cat <<'EOF'
Usage: scripts/ci/sanity.sh [options]

Options:
  --only N                 Run only stage N (1-6); may be repeated
  --skip N                 Skip stage N (1-6); may be repeated
  --require-formal         Fail instead of soft-skipping stage 4
  --require-image-build    Fail if stage 5 cannot run a real docker build
  -h, --help               Show this help

Stages:
  1  Unit tests (unittest discover, full assertion corpus)
  2  FPV TCL generation smoke (SV + Verilog-95 suites)
  3  Coverage fast path / hierarchical opt-in
  4  Formal smoke: SVApshot ft_fpnew_top + run_vcf_batch
  5  Docker packaging dry-check
  6  Large SVA → CNF conversion over the assertion corpus
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --only)
            ONLY="${ONLY} ${2:-}"
            shift 2
            ;;
        --skip)
            SKIP="${SKIP} ${2:-}"
            shift 2
            ;;
        --require-formal)
            export CI_REQUIRE_FORMAL=1
            shift
            ;;
        --require-image-build)
            export CI_REQUIRE_IMAGE_BUILD=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'error: unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

should_run() {
    local n="$1"
    if [[ -n "${ONLY// /}" ]]; then
        [[ " ${ONLY} " == *" ${n} "* ]]
        return
    fi
    if [[ " ${SKIP} " == *" ${n} "* ]]; then
        return 1
    fi
    return 0
}

stage_title() {
    case "$1" in
        1) printf '%s' 'Unit tests' ;;
        2) printf '%s' 'TCL / Verilog harness smoke' ;;
        3) printf '%s' 'Coverage controls' ;;
        4) printf '%s' 'Formal smoke on fpnew_top' ;;
        5) printf '%s' 'Docker packaging dry-check' ;;
        6) printf '%s' 'SVA → CNF conversion' ;;
        *) printf '%s' "Stage ${1}" ;;
    esac
}

status_glyph() {
    case "$1" in
        OK) printf '✅' ;;
        SKIP|SKIPPED*) printf '⏭️' ;;
        FAIL*) printf '❌' ;;
        *) printf '%s' "$1" ;;
    esac
}

declare -a STAGE_SCRIPTS=(
    "01_unit_tests.sh"
    "02_tcl_generation.sh"
    "03_coverage_controls.sh"
    "04_formal_smoke.sh"
    "05_docker_packaging.sh"
    "06_sva_cnf_conversion.sh"
)

declare -a RESULT_CODES=()
FAIL=0

ci_cd_repo
ci_pip_install_if_needed

RESULTS_DIR="${REPO_ROOT}/ci_artifacts"
mkdir -p "${RESULTS_DIR}"

for i in 1 2 3 4 5 6; do
    script="${SCRIPT_DIR}/${STAGE_SCRIPTS[$((i - 1))]}"
    if ! should_run "${i}"; then
        RESULT_CODES+=("SKIP")
        continue
    fi
    printf '\n============================================================ Stage %s start ============================================================\n' "${i}"
    printf '==> %s\n' "$(stage_title "${i}")"
    status_file="$(mktemp)"
    printf 'OK\n' >"${status_file}"
    set +e
    CI_STATUS_FILE="${status_file}" "${script}"
    exit_code=$?
    set -e
    stage_status="$(tr -d '[:space:]' <"${status_file}")"
    rm -f "${status_file}"
    if [[ ${exit_code} -ne 0 ]]; then
        RESULT_CODES+=("FAIL")
        FAIL=1
    elif [[ "${stage_status}" == "SKIP" ]]; then
        RESULT_CODES+=("SKIP")
    else
        RESULT_CODES+=("OK")
    fi
    printf '\n============================================================ Stage %s finish ============================================================\n' "${i}"
done

printf '\n======== Sanity Summary ========\n'
for i in 1 2 3 4 5 6; do
    code="${RESULT_CODES[$((i - 1))]}"
    printf '  %s %s: %s\n' "$(stage_title "${i}")" "${i}" "$(status_glyph "${code}")"
done
printf '================================\n'
printf 'Check the results at %s\n' "${RESULTS_DIR}"
printf '  formal smoke: %s/formal_smoke/\n' "${RESULTS_DIR}"
printf '  SVA→CNF:      %s/sva_cnf_conversion/\n' "${RESULTS_DIR}"
printf '================================\n'

exit "${FAIL}"
