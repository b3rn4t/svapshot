#!/usr/bin/env bash
# Launch sanity.sh in a fresh container from the host.
# Not needed when already inside vcformal — run ./scripts/ci/sanity.sh there.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
IMAGE="${CI_IMAGE:-python:3.11-bookworm}"

if ! command -v docker >/dev/null 2>&1; then
    printf 'error: docker not on PATH\n' >&2
    exit 1
fi

ENV_ARGS=()
for var in \
    SVAPSHOT_COVERAGE \
    SVAPSHOT_HIERARCHICAL_COVERAGE \
    SVAPSHOT_FML_MAX_TIME \
    SVAPSHOT_FORMAL_TIMEOUT_S \
    SVAPSHOT_FML_PROPERTY_TIME \
    VC_FORMAL_HOME \
    VC_STATIC_HOME \
    SNPSLMD_LICENSE_FILE \
    LM_LICENSE_FILE \
    CI_REQUIRE_FORMAL \
    CI_REQUIRE_IMAGE_BUILD \
    CI_PIP_REFRESH \
    INSTALLER_DIR
do
    if [[ -n "${!var:-}" ]]; then
        ENV_ARGS+=(-e "${var}=${!var}")
    fi
done

PIP_INSTALL=1
# Local vcformal already baked build/requirements.txt. Synopsys CI images
# (rl9-snps-vc, …) still need build/requirements-ci.txt at job start.
if [[ "${IMAGE}" == *vcformal* || "${CI_PIP_INSTALL:-}" == "0" ]]; then
    PIP_INSTALL=0
fi
if [[ "${IMAGE}" == *snps-vc* || "${IMAGE}" == *rl9-snps* ]]; then
    PIP_INSTALL=1
fi
if [[ "${CI_PIP_REFRESH:-0}" == "1" ]]; then
    PIP_INSTALL=1
fi

printf '==> docker run %s (CI_PIP_INSTALL=%s, user=%s:%s)\n' \
    "${IMAGE}" "${PIP_INSTALL}" "$(id -u)" "$(id -g)"

# Run as the host UID so ci_artifacts/ and any ft_* leftovers on the bind
# mount are not left owned by root. HOME=/tmp is enough for a one-shot CI run.
exec docker run --rm \
    --user "$(id -u):$(id -g)" \
    -e "HOME=/tmp" \
    -e "USER=$(id -un)" \
    -e "SVAPSHOT_ROOT=/work" \
    -e "PYTHONPATH=/work/src/core:/work/src/analysis:/work" \
    -e "SVALINT_ROOT=/work/SVALint" \
    -v "${REPO_ROOT}:/work" \
    -w /work \
    -e "CI_PIP_INSTALL=${PIP_INSTALL}" \
    "${ENV_ARGS[@]}" \
    "${IMAGE}" \
    ./scripts/ci/sanity.sh "$@"
