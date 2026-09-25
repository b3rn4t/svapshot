#!/bin/bash
# Environment for a SVApshot 0.1 checkout. Does not embed API keys.
set -euo pipefail

_svapshot_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export SVAPSHOT_ROOT="${SVAPSHOT_ROOT:-$_svapshot_root}"
export DUT_ROOT="${DUT_ROOT:-$SVAPSHOT_ROOT/modules}"

# Leave existing keys alone; only document the names.
: "${NVIDIA_API_KEY:=}"
: "${OPENAI_API_KEY:=}"
: "${CURSOR_API_KEY:=}"

export VC_FORMAL_HOME="${VC_FORMAL_HOME:-${VC_STATIC_HOME:-}}"
if [[ -n "${VC_FORMAL_HOME}" ]]; then
    export VC_STATIC_HOME="${VC_STATIC_HOME:-$VC_FORMAL_HOME}"
    case ":$PATH:" in
        *":$VC_FORMAL_HOME/bin:"*) ;;
        *) export PATH="$VC_FORMAL_HOME/bin:$PATH" ;;
    esac
fi

if [[ -f "${SVAPSHOT_ROOT}/SVALint/bin/svalint.py" ]]; then
    export SVALINT_ROOT="${SVALINT_ROOT:-$SVAPSHOT_ROOT/SVALint}"
fi

if [[ -d "${HOME}/.local/bin" ]]; then
    case ":$PATH:" in
        *":${HOME}/.local/bin:"*) ;;
        *) export PATH="${HOME}/.local/bin:$PATH" ;;
    esac
fi

echo "SVAPSHOT_ROOT=$SVAPSHOT_ROOT"
echo "Set NVIDIA_API_KEY / OPENAI_API_KEY / CURSOR_API_KEY in the environment; this script does not store them."
