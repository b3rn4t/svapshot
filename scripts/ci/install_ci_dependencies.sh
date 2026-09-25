#! /usr/bin/env bash
      
      export CI_PROJECT_DIR="${CI_PROJECT_DIR:-$PWD}"
      export VERIBLE_VERSION="v0.0-4080-ga0a8d8eb"
      
      set -euo pipefail
      echo "==> SVApshot CI bootstrap on $(hostname)"
      echo "    image python3: $(python3 --version 2>&1 || true)"
      echo "    VC_STATIC_HOME=${VC_STATIC_HOME:-<unset>}"
      echo "    VC_FORMAL_HOME=${VC_FORMAL_HOME:-<unset>}"

      # --- PATH for vcf (rl9-snps-vc sets VC_STATIC_HOME / PATH; be defensive) ---
      if [ -n "${VC_FORMAL_HOME:-}" ] && [ -d "${VC_FORMAL_HOME}/bin" ]; then
        export PATH="${VC_FORMAL_HOME}/bin:${PATH}"
      elif [ -n "${VC_STATIC_HOME:-}" ] && [ -d "${VC_STATIC_HOME}/bin" ]; then
        export VC_FORMAL_HOME="${VC_STATIC_HOME}"
        export PATH="${VC_STATIC_HOME}/bin:${PATH}"
      fi
      command -v vcf >/dev/null
      vcf -ID || true

      # --- Minimal OS packages the Synopsys image may still lack ---
      # python3-pip: requirements-ci.txt
      # git wget tar: submodule fetch, Verible
      if command -v dnf >/dev/null; then
        dnf install -y --setopt=install_weak_deps=False \
          python3-pip git wget tar gzip \
          || true
      fi

      # --- Verible (SVALint shells out to verible-verilog-syntax) ---
      if ! command -v verible-verilog-syntax >/dev/null; then
        echo "==> Installing Verible ${VERIBLE_VERSION}"
        wget -q \
          "https://github.com/chipsalliance/verible/releases/download/${VERIBLE_VERSION}/verible-${VERIBLE_VERSION}-linux-static-x86_64.tar.gz" \
          -O /tmp/verible.tar.gz
        tar xzf /tmp/verible.tar.gz -C /tmp
        install -m 0755 \
          "/tmp/verible-${VERIBLE_VERSION}/bin/verible-verilog-syntax" \
          /usr/local/bin/verible-verilog-syntax
        verible-verilog-syntax --version
      fi

      # --- Minimal pip surface for sanity.sh (no LLM providers) ---
      python3 -m pip install --upgrade pip
      python3 -m pip install --no-cache-dir -r build/requirements-ci.txt
      export PYTHONPATH="${CI_PROJECT_DIR}:${CI_PROJECT_DIR}/src/core:${CI_PROJECT_DIR}/src/analysis${PYTHONPATH:+:${PYTHONPATH}}"
      export CI_PIP_INSTALL=0
      echo "==> bootstrap done"

