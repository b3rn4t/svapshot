#!/bin/bash
# Run VC Formal FPV in batch mode (no GUI).
# Usage: ./run_vcf_batch.sh <DUT_MODULE_NAME>  (without file extension)
#
# Expects:
#   ft_<DUT>/FPV_vcf.tcl  – VC Formal Tcl script for the module
# Produces:
#   vcf_projs/<DUT>/vcf.log  – combined screen + message log (parsed by agent.py)
#
# Host Cursor/nohup sessions set TERM=dumb. The host Synopsys tree then
# exits immediately with "'dumb': unknown terminal type." DATE FPV must
# go through the vcformal docker (TERM=vt100 + TERMINFO), unless we are
# already inside that container.

if [ -z "$1" ]
then
    echo "Usage: ./run_vcf_batch.sh <DUT_MODULE_NAME> (without extension)"
    exit 1
fi

DUT=$1
PROJ_DIR="vcf_projs/${DUT}"
TCL_SCRIPT="ft_${DUT}/FPV_vcf.tcl"
LOG_FILE="${PROJ_DIR}/vcf.log"
SESSION_DIR="${PROJ_DIR}/vcst_rtdb"
VCF_HOME="${VC_FORMAL_HOME:-${VC_STATIC_HOME:-}}"
CONTAINER="${SVAPSHOT_VCF_DOCKER:-vcformal-dev}"
if [[ -z "$VCF_HOME" ]]; then
    echo "error: set VC_FORMAL_HOME or VC_STATIC_HOME" >&2
    exit 1
fi

mkdir -p "${PROJ_DIR}"

# A leftover session.lock means another vcf still owns this project, or a
# dead run left the lock behind. DATE cells must not queue behind that.
# The Python caller (vcf_session.prepare_vcf_session) removes a stale lock
# after killing leftovers; if the lock is still here, fail immediately.
if [[ "${SVAPSHOT_VCF_FAIL_FAST:-}" == "1" && -e "${SESSION_DIR}/session.lock" ]]; then
    echo "error: leftover VC Formal session lock at ${SESSION_DIR}/session.lock" >&2
    echo "error: refusing to wait; another session still owns vcst_rtdb" >&2
    exit 1
fi

run_vcf() {
    if [[ -f /.dockerenv ]]; then
        export TERM="${TERM:-vt100}"
        export TERMINFO="${TERMINFO:-/usr/share/terminfo}"
        export VC_FORMAL_HOME="$VCF_HOME"
        export VC_STATIC_HOME="${VC_STATIC_HOME:-$VCF_HOME}"
        export PATH="$VCF_HOME/bin:$PATH"
        vcf -session "${SESSION_DIR}" -no_restore -f "${TCL_SCRIPT}" -batch
        return
    fi
    if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -qx true; then
        echo "error: $CONTAINER is not running; start vcformal-dev before FPV" >&2
        exit 1
    fi
    docker exec \
        -e TERM=vt100 \
        -e TERMINFO=/usr/share/terminfo \
        -e VC_FORMAL_HOME="$VCF_HOME" \
        -e VC_STATIC_HOME="$VCF_HOME" \
        -e HOME="${HOME}" \
        -e USER="${USER}" \
        -e "SNPSLMD_LICENSE_FILE=${SNPSLMD_LICENSE_FILE:-}" \
        -e "SNPS_LICENSE_FILE=${SNPS_LICENSE_FILE:-}" \
        -e "LM_LICENSE_FILE=${LM_LICENSE_FILE:-}" \
        -e "SNPSLMD_QUEUE=${SNPSLMD_QUEUE:-true}" \
        -w "$PWD" \
        "$CONTAINER" \
        "$VCF_HOME/bin/vcf" \
        -session "${SESSION_DIR}" \
        -no_restore \
        -f "${TCL_SCRIPT}" \
        -batch
}

# Optional host lock. Parallel jobs set SVAPSHOT_VCF_LOCK=off and each
# uses its own container (one job ↔ one container). The default lock
# remains for leftover workers that still share vcformal-dev.
LOCK="${SVAPSHOT_VCF_LOCK:-/tmp/svapshot-vcf.lock}"
if [[ "$LOCK" != "off" && "$LOCK" != "none" && -n "$LOCK" ]]; then
    LOCK_WAIT="${SVAPSHOT_VCF_LOCK_WAIT:-7200}"
    exec 9>"$LOCK"
    if ! flock -w "$LOCK_WAIT" 9; then
        echo "error: timed out after ${LOCK_WAIT}s waiting for $LOCK" >&2
        exit 1
    fi
fi

# stdin comes from /dev/null: vcf keeps the terminal on its stdin otherwise, and
# a read from it by a background process group earns the whole tool tree a
# SIGTTIN. Exit code is suppressed (|| true) because VC Formal exits non-zero
# on compile/proof failures; the agent inspects the log.
run_vcf < /dev/null > "${LOG_FILE}" 2>&1 || true
