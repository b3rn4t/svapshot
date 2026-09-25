#!/bin/bash
#alias jg='<LICENSE_PATH>/jasper_2021.03/bin/jg'; # Or the version that you are using

if [ -z "$1" ]
then
    echo "Param is empty, use ./run.sh <DUT> (without .v)"
else
    # stdin from /dev/null: this runs in the background, and a background
    # process group that reads the terminal is stopped by SIGTTIN.
    jg -batch -tcl ft_$1/FPV.tcl -proj projs/$1 < /dev/null &
fi
