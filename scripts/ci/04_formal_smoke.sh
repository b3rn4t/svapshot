#!/usr/bin/env bash
# Stage 4: SVApshot ft_fpnew_top + run_vcf_batch.sh smoke (coverage off).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ci_cd_repo
ci_set_status OK

FPNEW_RTL="${REPO_ROOT}/benchmarks/sargantana/rtl/datapath/rtl/exe_stage/rtl/fpu/src/fpnew_top.sv"
WORKDIR="${REPO_ROOT}/ci_artifacts/formal_smoke"
TIMEOUT_S="${SVAPSHOT_FORMAL_TIMEOUT_S:-900}"

if [[ ! -f "${FPNEW_RTL}" ]]; then
    ci_skip "fpnew_top RTL missing (${FPNEW_RTL})" CI_REQUIRE_FORMAL
fi

if ! ci_need_cmd python3; then
    printf 'error: python3 required\n' >&2
    exit 1
fi

# Non-interactive shells do not load the interactive `vcf -full64` alias.
if [[ -n "${VC_FORMAL_HOME:-}" && -d "${VC_FORMAL_HOME}/bin" ]]; then
    case ":${PATH}:" in
        *":${VC_FORMAL_HOME}/bin:"*) ;;
        *) export PATH="${VC_FORMAL_HOME}/bin:${PATH}" ;;
    esac
fi

if ! ci_need_cmd vcf; then
    ci_skip "vcf not on PATH" CI_REQUIRE_FORMAL
fi

mkdir -p "${WORKDIR}"
# Fresh smoke tree each run so leftovers cannot mask a broken harness path.
rm -rf "${WORKDIR}/ft_fpnew_top" "${WORKDIR}/vcf_projs/fpnew_top"

ci_log "SVApshot harness → ${WORKDIR}/ft_fpnew_top"
python3 "${SCRIPT_DIR}/build_fpnew_harness.py" --workdir "${WORKDIR}"

export SVAPSHOT_COVERAGE="${SVAPSHOT_COVERAGE:-0}"
export SVAPSHOT_HIERARCHICAL_COVERAGE="${SVAPSHOT_HIERARCHICAL_COVERAGE:-0}"
export SVAPSHOT_FML_MAX_TIME="${SVAPSHOT_FML_MAX_TIME:-120}"
export SVAPSHOT_FORMAL_TIMEOUT_S="${TIMEOUT_S}"

ci_log "run_vcf_batch.sh fpnew_top (timeout ${TIMEOUT_S}s, coverage=${SVAPSHOT_COVERAGE})"
(
    cd "${WORKDIR}"
    # Prefer the in-tree launcher; fall back to PATH vcf -full64 if needed.
    if command -v timeout >/dev/null 2>&1; then
        timeout --signal=TERM --kill-after=30 "${TIMEOUT_S}" \
            bash "${REPO_ROOT}/fpv_app_scripts/run_vcf_batch.sh" fpnew_top
    else
        bash "${REPO_ROOT}/fpv_app_scripts/run_vcf_batch.sh" fpnew_top
    fi
)

LOG="${WORKDIR}/vcf_projs/fpnew_top/vcf.log"
if [[ ! -s "${LOG}" ]]; then
    printf 'error: expected non-empty log at %s\n' "${LOG}" >&2
    exit 1
fi

ci_log "formal smoke OK (${LOG}, $(wc -c < "${LOG}") bytes)"
