#!/usr/bin/env python3
"""
Script to collect duplicate assertion data for all final experiments.
Runs duplicate detection for all modules across specified timestamps.
"""

import os
import subprocess
import sys
from pathlib import Path

def get_module_name_from_experiment(experiment_dir):
    """Extract the module name from experiment directory name."""
    # Remove ft_ prefix and extract module name
    # e.g., ft_hl_mul_unit_20250824_140034_gpt_41_mini -> hl_mul_unit
    parts = experiment_dir.split('_')
    if parts[0] == 'ft':
        # Look for timestamp pattern YYYYMMDD_HHMMSS
        # Timestamp will be two consecutive parts: YYYYMMDD and HHMMSS
        for i in range(len(parts) - 1):
            if (len(parts[i]) == 8 and parts[i].startswith('202') and parts[i].isdigit() and
                len(parts[i+1]) == 6 and parts[i+1].isdigit()):
                # Found timestamp starting at index i
                # Module name is everything between ft_ and timestamp
                if i > 1:
                    module_name = '_'.join(parts[1:i])
                    return module_name
                break
    
    return None

def get_model_name_from_experiment(experiment_dir):
    """Extract the model name from experiment directory name."""
    if experiment_dir.endswith('_gpt_41_mini'):
        return 'gpt41'
    elif experiment_dir.endswith('_llama_33_70b'):
        return 'll70b'
    elif experiment_dir.endswith('_llama_405b'):
        return 'll405b'
    return 'unknown'

def run_duplicate_analysis():
    """Run duplicate analysis for all experiments."""
    
    # Timestamps to analyze
    timestamps = ['20250822_173324', '20250824_140034', '20250823_014948', '20250825_232658']
    
    # Base directory
    base_dir = Path('svapshot_v1_results')
    
    if not base_dir.exists():
        print(f"Error: {base_dir} directory not found!")
        return
    
    # Collect all experiment directories for the specified timestamps
    experiments = []
    for timestamp in timestamps:
        pattern = f"ft_*_{timestamp}_*"
        matching_dirs = list(base_dir.glob(pattern))
        experiments.extend(matching_dirs)
    
    if not experiments:
        print("No experiment directories found for the specified timestamps!")
        return
    
    # Sort experiments by timestamp, then module, then model
    experiments.sort(key=lambda x: (x.name.split('_')[-3], x.name))
    
    print(f"Found {len(experiments)} experiments to analyze:")
    for exp in experiments:
        print(f"  - {exp.name}")
    print()
    
    # Run analysis for each experiment
    successful_analyses = 0
    failed_analyses = []
    
    for exp_dir in experiments:
        module_name = get_module_name_from_experiment(exp_dir.name)
        model_name = get_model_name_from_experiment(exp_dir.name)
        
        if not module_name:
            print(f"⚠️  Could not extract module name from {exp_dir.name}")
            continue
        
        # Construct SVA file path
        sva_file = exp_dir / 'sva' / 'versions' / f'{module_name}_prop_after_semantic_correction.sv'
        
        if not sva_file.exists():
            print(f"⚠️  SVA file not found: {sva_file}")
            failed_analyses.append(f"{exp_dir.name} - SVA file missing")
            continue
        
        # Construct output filename
        # Format: {module}_duplicates_{model}
        output_file = f"{module_name}_duplicates_{model_name}"
        
        # Run duplicate detection
        detector = Path(__file__).resolve().with_name('detect_duplicates.py')
        cmd = [sys.executable, str(detector), str(sva_file)]
        
        print(f"🔍 Analyzing {exp_dir.name} -> {output_file}")
        
        try:
            with open(output_file, 'w') as f:
                result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, text=True)
            
            if result.returncode == 0:
                print(f"✅ Success: {output_file}")
                successful_analyses += 1
            else:
                print(f"❌ Failed: {exp_dir.name}")
                print(f"   Error: {result.stderr}")
                failed_analyses.append(f"{exp_dir.name} - Command failed: {result.stderr}")
                
        except Exception as e:
            print(f"❌ Exception for {exp_dir.name}: {e}")
            failed_analyses.append(f"{exp_dir.name} - Exception: {e}")
    
    # Summary
    print("\n" + "="*80)
    print("ANALYSIS SUMMARY")
    print("="*80)
    print(f"Total experiments: {len(experiments)}")
    print(f"Successful analyses: {successful_analyses}")
    print(f"Failed analyses: {len(failed_analyses)}")
    
    if failed_analyses:
        print("\nFailed analyses:")
        for failure in failed_analyses:
            print(f"  - {failure}")
    
    print(f"\nOutput files generated:")
    # List all generated output files
    output_pattern = "*_duplicates_*"
    output_files = list(Path('.').glob(output_pattern))
    output_files.sort()
    
    for output_file in output_files:
        size = output_file.stat().st_size
        print(f"  - {output_file.name} ({size} bytes)")
    
    print(f"\nTo view results, use commands like:")
    print(f"  cat {output_files[0].name if output_files else 'module_duplicates_model'}")
    print(f"  grep 'Redundancy rate' *_duplicates_*")

if __name__ == "__main__":
    run_duplicate_analysis() 