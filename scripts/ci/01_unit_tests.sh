#!/usr/bin/env bash
# Stage 1: full unit-test discovery under tests/.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ci_cd_repo
ci_set_status OK
ci_need_cmd python3 || { ci_set_status FAIL; printf 'error: python3 required\n' >&2; exit 1; }

# Sanity defaults this to 1. Local override SVAPSHOT_FULL_CORPUS=0 is allowed
# for a faster discover; GitLab (CI=true) and SVAPSHOT_REQUIRE_FULL_CORPUS=1
# must never silently run the 60-assertion corpus slice.
export SVAPSHOT_FULL_CORPUS="${SVAPSHOT_FULL_CORPUS:-1}"
if [[ "${CI:-}" == "true" || "${CI:-}" == "1" || "${SVAPSHOT_REQUIRE_FULL_CORPUS:-}" == "1" ]]; then
    if [[ "${SVAPSHOT_FULL_CORPUS}" != "1" ]]; then
        printf 'error: CI must run the full assertion corpus (SVAPSHOT_FULL_CORPUS=1, got %s)\n' \
            "${SVAPSHOT_FULL_CORPUS}" >&2
        exit 1
    fi
    export SVAPSHOT_REQUIRE_FULL_CORPUS=1
fi

ci_log "unit tests (visual discover, SVAPSHOT_FULL_CORPUS=${SVAPSHOT_FULL_CORPUS})"
# Same as: python3 -m unittest discover -s tests
ci_run_unittest --discover tests
