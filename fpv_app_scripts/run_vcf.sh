#!/bin/bash
# Run VC Formal FPV in batch mode (no GUI).
# Usage: ./run_vcf_batch.sh <DUT_MODULE_NAME>  (without file extension)
#
# Expects:
#   ft_<DUT>/FPV_vcf.tcl  – VC Formal Tcl script for the module
# Produces:
#   vcf_projs/<DUT>/vcf.log  – combined screen + message log (parsed by agent.py)

if [ -z "$1" ]
then
    echo "Usage: ./run_vcf.sh <DUT_MODULE_NAME> (without extension)"
    exit 1
fi

DUT=$1
PROJ_DIR="vcf_projs/${DUT}"
TCL_SCRIPT="ft_${DUT}/FPV_vcf.tcl"
LOG_FILE="${PROJ_DIR}/vcf.log"

mkdir -p "${PROJ_DIR}"

# Run VC Formal in batch mode.
# -batch : run non-interactively and exit when the Tcl script completes.
# Shell redirection captures both stdout and stderr (PROP_I_RESULT messages,
# compile errors, etc.) into LOG_FILE.  The -o flag does not exist in vcf;
# use -output_log_file if you prefer a vcf-native flag, but plain redirection
# is simpler and equally complete.
# Exit code is suppressed (|| true) because VC Formal exits non-zero on
# compile/proof failures; the agent inspects the log to determine the outcome.
vcf -gui -f "${TCL_SCRIPT}"