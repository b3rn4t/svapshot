#!/bin/bash

# Script to collect duplicate assertion data for all final experiments
# Runs duplicate detection for all modules across specified timestamps

echo "Starting duplicate analysis collection..."
echo "====================================="

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"

# Timestamps to analyze
timestamps=("20250822_173324" "20250824_140034" "20250823_014948" "20250825_232658")

# Base directory
base_dir="$REPO_ROOT/svapshot_v1_results"

# Check if base directory exists
if [ ! -d "$base_dir" ]; then
    echo "Error: $base_dir directory not found!"
    exit 1
fi

# Counter for successful analyses
successful=0
failed=0

# Function to extract module name from experiment directory
get_module_name() {
    local exp_dir="$1"
    # Remove ft_ prefix and extract module name
    # e.g., ft_hl_mul_unit_20250824_140034_gpt_41_mini -> hl_mul_unit
    
    # Split by underscores and find timestamp
    IFS='_' read -ra parts <<< "$exp_dir"
    
    if [ "${parts[0]}" = "ft" ]; then
        # Find timestamp (YYYYMMDD_HHMMSS pattern)
        for i in "${!parts[@]}"; do
            if [[ "${parts[i]}" =~ ^202[0-9]{5}$ ]] && [[ "${parts[i+1]}" =~ ^[0-9]{6}$ ]]; then
                # Found timestamp starting at index i
                # Module name is everything between ft_ and timestamp
                if [ $i -gt 1 ]; then
                    module_parts=("${parts[@]:1:$((i-1))}")
                    printf '%s\n' "${module_parts[*]}" | tr ' ' '_'
                    return
                fi
                break
            fi
        done
    fi
}

# Function to get model abbreviation
get_model_abbrev() {
    local exp_dir="$1"
    if [[ "$exp_dir" == *"_gpt_41_mini" ]]; then
        echo "gpt41"
    elif [[ "$exp_dir" == *"_llama_33_70b" ]]; then
        echo "ll70b"
    elif [[ "$exp_dir" == *"_llama_405b" ]]; then
        echo "ll405b"
    else
        echo "unknown"
    fi
}

# Process all experiment directories
for timestamp in "${timestamps[@]}"; do
    echo "Processing timestamp: $timestamp"
    
    # Find all experiment directories for this timestamp
    for exp_dir in "$base_dir"/ft_*_${timestamp}_*; do
        if [ -d "$exp_dir" ]; then
            exp_name=$(basename "$exp_dir")
            echo "  Found experiment: $exp_name"
            
            # Extract module name and model
            module_name=$(get_module_name "$exp_name")
            model_abbrev=$(get_model_abbrev "$exp_name")
            
            if [ -z "$module_name" ]; then
                echo "    Warning: Could not extract module name from $exp_name"
                ((failed++))
                continue
            fi
            
            # Construct SVA file path
            sva_file="$exp_dir/sva/versions/${module_name}_prop_after_extension.sv"
            
            if [ ! -f "$sva_file" ]; then
                echo "    Warning: SVA file not found: $sva_file"
                ((failed++))
                continue
            fi
            
            # Construct output filename
            output_file="${module_name}_duplicates_${model_abbrev}"
            
            echo "    Analyzing: $exp_name -> $output_file"
            
            # Run duplicate detection
            if python3 "$SCRIPT_DIR/detect_duplicates.py" "$sva_file" > "$output_file" 2>&1; then
                echo "    Success: $output_file ($(wc -c < "$output_file") bytes)"
                ((successful++))
            else
                echo "    Failed: Error running duplicate detection"
                ((failed++))
            fi
        fi
    done
done

echo ""
echo "====================================="
echo "ANALYSIS COMPLETE"
echo "====================================="
echo "Successful analyses: $successful"
echo "Failed analyses: $failed"
echo "Total: $((successful + failed))"

# List generated files
echo ""
echo "Generated output files:"
ls -la *_duplicates_* 2>/dev/null | while read -r line; do
    echo "  $line"
done

echo ""
echo "To view results:"
echo "  grep 'Redundancy rate' *_duplicates_*"
echo "  cat specific_module_duplicates_model" 