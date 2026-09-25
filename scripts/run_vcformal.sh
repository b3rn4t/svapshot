#!/usr/bin/env bash
# Launch the local vcformal development container as the host user.
#
# Bind-mounted writes (ft_*/, vcf_projs/, ci_artifacts/, …) then keep the host
# UID/GID, so there is no chown loop after every run. Requires an image built
# with matching USER_UID/USER_GID (build/build.sh passes them by default).
#
# Canonical container checkout path is SVAPSHOT_ROOT (default /aisva). Do not
# mount under /root — see build/SVAPSHOT_ROOT.md.
#
# Usage:
#   ./scripts/run_vcformal.sh
#   ./scripts/run_vcformal.sh bash -lc './scripts/ci/sanity.sh'
#
# Environment overrides:
#   IMAGE, REPO_HOST, SVAPSHOT_ROOT, VC_HOST_INSTALL, DISPLAY,
#   CONTAINER_NAME (default vcformal-dev; if that name is already running,
#   the next free vcformal-dev-N is used so invocations can run in parallel)

set -euo pipefail

IMAGE="${IMAGE:-vcformal:latest}"
CONTAINER_NAME="${CONTAINER_NAME:-vcformal-dev}"
DETACH="${DETACH:-0}"

# Reuse a free name: drop a leftover stopped container (reboot / crash),
# and if the name is still live start vcformal-dev-2, -3, …
if [[ -z "${CONTAINER_NAME_LOCKED:-}" ]]; then
    base="${CONTAINER_NAME}"
    if docker inspect "${base}" >/dev/null 2>&1; then
        if docker inspect -f '{{.State.Running}}' "${base}" 2>/dev/null | grep -qx true; then
            n=2
            while docker inspect "${base}-${n}" >/dev/null 2>&1; do
                if docker inspect -f '{{.State.Running}}' "${base}-${n}" 2>/dev/null | grep -qx true; then
                    n=$((n + 1))
                    continue
                fi
                docker rm "${base}-${n}" >/dev/null
                break
            done
            CONTAINER_NAME="${base}-${n}"
        else
            docker rm "${base}" >/dev/null
        fi
    fi
fi
printf 'run_vcformal: container %s\n' "${CONTAINER_NAME}" >&2
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"
HOST_USER="$(id -un)"
HOST_HOME="${HOME}"

REPO_HOST="${REPO_HOST:-${PWD}}"
# Prefer SVAPSHOT_ROOT; keep REPO_CONTAINER as a deprecated alias.
SVAPSHOT_ROOT="${SVAPSHOT_ROOT:-${REPO_CONTAINER:-/aisva}}"
VC_HOST_INSTALL="${VC_HOST_INSTALL:-${HOST_HOME}/synopsys/ins}"

xauth="${XAUTHORITY:-${HOST_HOME}/.Xauthority}"
if [[ ! -f "${xauth}" ]]; then
    printf 'run_vcformal: no X authority file at %s; the GUI will not open\n' \
        "${xauth}" >&2
    xauth=/dev/null
fi

docker_args=()
if [[ "${DETACH}" == "1" ]]; then
    docker_args+=(-d)
else
    docker_args+=(-it --rm)
fi
docker_args+=(
    --name "${CONTAINER_NAME}"
    --user "${HOST_UID}:${HOST_GID}"
    --network host
    -e "HOME=${HOST_HOME}"
    -e "USER=${HOST_USER}"
    -e "DISPLAY=${DISPLAY:-}"
    -e "XAUTHORITY=${HOST_HOME}/.Xauthority"
    -e "SVAPSHOT_ROOT=${SVAPSHOT_ROOT}"
    -e "PYTHONPATH=${SVAPSHOT_ROOT}/src/core:${SVAPSHOT_ROOT}/src/analysis:${SVAPSHOT_ROOT}"
    -e "SVALINT_ROOT=${SVAPSHOT_ROOT}/SVALint"
    -e "SNPSLMD_LICENSE_FILE=${SNPSLMD_LICENSE_FILE:-}"
    -e "SNPS_LICENSE_FILE=${SNPS_LICENSE_FILE:-}"
    -e "LM_LICENSE_FILE=${LM_LICENSE_FILE:-}"
    -e "SNPSLMD_QUEUE=${SNPSLMD_QUEUE:-true}"
    -e GIT_CONFIG_COUNT=1
    -e GIT_CONFIG_KEY_0=safe.directory
    -e GIT_CONFIG_VALUE_0='*'
    -v "${HOST_HOME}:${HOST_HOME}"
    -v "${REPO_HOST}:${SVAPSHOT_ROOT}"
    -v "${VC_HOST_INSTALL}:${VC_HOST_INSTALL}"
    -v /tmp/.X11-unix:/tmp/.X11-unix
    -v "${xauth}:${HOST_HOME}/.Xauthority:ro"
    -w "${SVAPSHOT_ROOT}"
)

# Optional sibling checkouts — mount outside /root so --user can reach them.
for pair in \
    "${HOST_HOME}/bsc_repos/ttttt/FPUV-DV:/fpudv" \
    "${HOST_HOME}/bsc_repos/vcformal:/vcformal" \
    "${HOST_HOME}/bsc_repos/AccLEC:/acclec"
do
    host_path="${pair%%:*}"
    cont_path="${pair#*:}"
    if [[ -d "${host_path}" ]]; then
        docker_args+=(-v "${host_path}:${cont_path}")
    fi
done

if docker volume inspect cursor-server >/dev/null 2>&1; then
    docker_args+=(-v cursor-server:/tmp/cursor-server)
fi

exec docker run "${docker_args[@]}" "${IMAGE}" "$@"
