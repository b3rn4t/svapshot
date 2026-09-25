#!/usr/bin/env bash
# Stage 5: Docker packaging dry-check (no Synopsys media required by default).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ci_cd_repo
ci_set_status OK

FILES=(
    build/dockerfile
    build/build.sh
    hpc/dockerfile
    hpc/build.sh
    build/requirements.txt
    build/requirements-ci.txt
    hpc/requirements.txt
)

for f in "${FILES[@]}"; do
    if [[ ! -f "${REPO_ROOT}/${f}" ]]; then
        printf 'error: missing %s\n' "${f}" >&2
        exit 1
    fi
done

ci_log "bash -n on build scripts"
bash -n "${REPO_ROOT}/build/build.sh"
bash -n "${REPO_ROOT}/hpc/build.sh"

# Lightweight Dockerfile sanity: FROM line present, not empty.
for df in build/dockerfile hpc/dockerfile; do
    if ! grep -Eq '^[[:space:]]*FROM[[:space:]]+' "${REPO_ROOT}/${df}"; then
        printf 'error: %s has no FROM instruction\n' "${df}" >&2
        exit 1
    fi
done

if [[ "${CI_REQUIRE_IMAGE_BUILD}" == "1" ]]; then
    if ! ci_need_cmd docker; then
        printf 'error: --require-image-build needs docker on PATH\n' >&2
        exit 1
    fi
    if [[ -z "${INSTALLER_DIR:-}" ]]; then
        printf 'error: --require-image-build needs INSTALLER_DIR\n' >&2
        exit 1
    fi
    ci_log "running build/build.sh (INSTALLER_DIR=${INSTALLER_DIR})"
    "${REPO_ROOT}/build/build.sh"
else
    ci_log "docker packaging dry-check OK (pass --require-image-build for a real build)"
fi
