#!/usr/bin/env bash
# Initialise the submodules CI needs. Fail if any required tree cannot be cloned.
#
# Nested sargantana remotes use GitLab-sibling URLs (`../csr.git`, `../mmu.git`).
# Those are public on GitHub (bsc-loca/{csr,mmu}); rewrite them before recursing.
# Do not fetch private GitLab siblings.
#
# Usage (repo root, or sourced from .gitlab-ci.yml):
#   ./scripts/ci/update_submodules.sh
#   source scripts/ci/update_submodules.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

GITHUB_SARGANTANA_ORG="${GITHUB_SARGANTANA_ORG:-bsc-loca}"

log() {
    printf '==> %s\n' "$*"
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

# ../foo.git → https://github.com/<org>/foo.git (GitLab group-clone sibling layout).
rewrite_relative_gitmodules() {
    local root="$1"
    local org="${2:-${GITHUB_SARGANTANA_ORG}}"
    [[ -d "${root}" ]] || return 0
    local gm dir key url
    while IFS= read -r -d '' gm; do
        dir="$(dirname "${gm}")"
        if grep -qE 'url = \.\./' "${gm}"; then
            log "Rewriting relative submodule URLs in ${gm#"${REPO_ROOT}"/}"
            sed -i -E \
                "s#url = \\.\\.\\/([A-Za-z0-9._-]+)\\.git#url = https://github.com/${org}/\\1.git#g" \
                "${gm}"
        fi
        git -C "${dir}" rev-parse --is-inside-work-tree >/dev/null 2>&1 || continue
        while read -r key url; do
            git -C "${dir}" config "${key}" "${url}"
        done < <(git -C "${dir}" config --file .gitmodules --get-regexp '^submodule\..*\.url$' || true)
        git -C "${dir}" submodule sync --recursive >/dev/null
    done < <(find "${root}" -name .gitmodules -print0)
}

init_required() {
    local path="$1"
    shift
    log "Initialising ${path}"
    git submodule update --init "$@" "${path}" \
        || die "could not initialise submodule ${path}"
    if [[ ! -e "${path}" ]] || [[ -z "$(ls -A "${path}" 2>/dev/null || true)" ]]; then
        die "submodule ${path} is empty after init"
    fi
}

# Populate benchmarks/2605.06434/fifo from the OpenCores generic_fifos submodule
# when the curated flat copy is absent (publication.yaml points at fifo/*.v).
materialize_opencores_fifo() {
    local dest="${REPO_ROOT}/benchmarks/2605.06434/fifo"
    local src="${REPO_ROOT}/benchmarks/generic_fifos/rtl/verilog"
    if [[ -f "${dest}/generic_fifo_sc_a.v" ]]; then
        return 0
    fi
    [[ -d "${src}" ]] || die "OpenCores generic_fifos RTL missing at ${src#"${REPO_ROOT}"/}"
    log "Materialising OpenCores FIFO RTL into benchmarks/2605.06434/fifo"
    mkdir -p "${dest}"
    cp -a "${src}"/. "${dest}"/
    [[ -f "${dest}/generic_fifo_sc_a.v" ]] \
        || die "materialised fifo tree is missing generic_fifo_sc_a.v"
}

log "Initialising SVALint submodule"
git submodule update --init --depth 1 SVALint \
    || die "could not initialise submodule SVALint"
export SVALINT_ROOT="${CI_PROJECT_DIR:-${REPO_ROOT}}/SVALint"

log "Initialising sargantana"
git submodule update --init benchmarks/sargantana \
    || die "could not initialise submodule benchmarks/sargantana"
rewrite_relative_gitmodules "${REPO_ROOT}/benchmarks/sargantana"
log "Recursing the full sargantana tree (public GitHub remotes)"
git -C benchmarks/sargantana submodule update --init --recursive \
    || die "recursive sargantana init failed (after rewriting relative remotes to GitHub)"
# Nested clones may introduce further relative URLs; rewrite and finish.
rewrite_relative_gitmodules "${REPO_ROOT}/benchmarks/sargantana"
git -C benchmarks/sargantana submodule update --init --recursive \
    || die "recursive sargantana init failed on nested remotes"

if git -C benchmarks/sargantana submodule status --recursive | grep -q '^-'; then
    git -C benchmarks/sargantana submodule status --recursive >&2
    die "sargantana still has uninitialised nested submodules"
fi

for nested in \
    rtl/csr \
    rtl/mmu \
    rtl/common_cells \
    rtl/datapath/rtl/exe_stage/rtl/fpu
do
    [[ -e "benchmarks/sargantana/${nested}" ]] \
        && [[ -n "$(ls -A "benchmarks/sargantana/${nested}" 2>/dev/null || true)" ]] \
        || die "sargantana nested tree missing after recurse: ${nested}"
done

init_required benchmarks/AssertLLM2 --depth 1
init_required benchmarks/FVEval --depth 1
init_required benchmarks/generic_fifos --depth 1
init_required svatools/ChIRAAG --depth 1
init_required svatools/SANGAM --depth 1

materialize_opencores_fifo

log "CI submodules ready"
