#!/usr/bin/env python3
"""
Script to summarize duplicate analysis results from all experiments.
Generates a comprehensive report of redundancy patterns across modules and models.
"""

import re
from pathlib import Path
import sys

def parse_duplicate_file(filepath):
    """Parse a duplicate analysis file and extract key metrics."""
    try:
        with open(filepath, 'r') as f:
            content = f.read()
        
        # Extract basic metrics
        total_assertions = 0
        unique_properties = 0
        duplicate_patterns = 0
        duplicate_instances = 0
        redundant_assertions = 0
        redundancy_rate = 0.0
        
        # Parse summary section
        lines = content.split('\n')
        in_summary = False
        
        for line in lines:
            line = line.strip()
            
            if line == "Summary:":
                in_summary = True
                continue
            
            if in_summary:
                if line.startswith("Total assertions analyzed:"):
                    total_assertions = int(line.split(":")[1].strip())
                elif line.startswith("Total unique properties:"):
                    unique_properties = int(line.split(":")[1].strip())
                elif line.startswith("Duplicate property patterns:"):
                    duplicate_patterns = int(line.split(":")[1].strip())
                elif line.startswith("Total duplicate assertion instances:"):
                    duplicate_instances = int(line.split(":")[1].strip())
                elif line.startswith("Redundant assertions (could be removed):"):
                    redundant_assertions = int(line.split(":")[1].strip())
                elif line.startswith("Redundancy rate:"):
                    rate_str = line.split(":")[1].strip().replace('%', '')
                    redundancy_rate = float(rate_str)
        
        # Check if no duplicates found
        if "No duplicate assertion properties found" in content:
            return {
                'total_assertions': total_assertions,
                'unique_properties': unique_properties,
                'duplicate_patterns': 0,
                'duplicate_instances': 0,
                'redundant_assertions': 0,
                'redundancy_rate': 0.0,
                'has_duplicates': False
            }
        
        return {
            'total_assertions': total_assertions,
            'unique_properties': unique_properties,
            'duplicate_patterns': duplicate_patterns,
            'duplicate_instances': duplicate_instances,
            'redundant_assertions': redundant_assertions,
            'redundancy_rate': redundancy_rate,
            'has_duplicates': duplicate_patterns > 0
        }
        
    except Exception as e:
        print(f"Error parsing {filepath}: {e}")
        return None

def extract_module_and_model(filename):
    """Extract module name and model from filename."""
    # Remove .py extension if present and split by '_duplicates_'
    name = filename.replace('.py', '')
    if '_duplicates_' in name:
        parts = name.split('_duplicates_')
        module = parts[0]
        model = parts[1]
        return module, model
    return None, None

def generate_summary_report():
    """Generate a comprehensive summary report."""
    
    # Find all duplicate analysis files
    duplicate_files = list(Path('.').glob('*_duplicates_*'))
    duplicate_files = [f for f in duplicate_files if f.name != 'mul_duplicates_ll70b']  # Exclude old file
    
    if not duplicate_files:
        print("No duplicate analysis files found!")
        return
    
    # Parse all files
    results = {}
    total_experiments = 0
    experiments_with_duplicates = 0
    
    for filepath in duplicate_files:
        module, model = extract_module_and_model(filepath.name)
        if not module or not model:
            continue
            
        data = parse_duplicate_file(filepath)
        if data is None:
            continue
            
        total_experiments += 1
        if data['has_duplicates']:
            experiments_with_duplicates += 1
            
        if module not in results:
            results[module] = {}
        results[module][model] = data
    
    # Generate report
    print("="*100)
    print("COMPREHENSIVE DUPLICATE ANALYSIS SUMMARY")
    print("="*100)
    print(f"Total experiments analyzed: {total_experiments}")
    print(f"Experiments with duplicates: {experiments_with_duplicates}")
    print(f"Experiments with no duplicates: {total_experiments - experiments_with_duplicates}")
    print(f"Percentage with duplicates: {(experiments_with_duplicates/total_experiments)*100:.1f}%")
    print()
    
    # Summary by module
    print("RESULTS BY MODULE:")
    print("-" * 100)
    
    modules = sorted(results.keys())
    models = ['gpt41', 'll70b', 'll405b']
    
    # Header
    print(f"{'Module':<25} {'Model':<8} {'Total':<6} {'Unique':<6} {'Dupe':<5} {'Redund':<6} {'Rate%':<6}")
    print("-" * 100)
    
    module_stats = {}
    model_stats = {}
    
    for module in modules:
        module_total_assertions = 0
        module_total_redundant = 0
        
        for model in models:
            if model in results[module]:
                data = results[module][model]
                
                # Track module stats
                module_total_assertions += data['total_assertions']
                module_total_redundant += data['redundant_assertions']
                
                # Track model stats
                if model not in model_stats:
                    model_stats[model] = {'total_assertions': 0, 'total_redundant': 0, 'experiments': 0}
                model_stats[model]['total_assertions'] += data['total_assertions']
                model_stats[model]['total_redundant'] += data['redundant_assertions']
                model_stats[model]['experiments'] += 1
                
                print(f"{module:<25} {model:<8} {data['total_assertions']:<6} {data['unique_properties']:<6} "
                      f"{data['duplicate_patterns']:<5} {data['redundant_assertions']:<6} {data['redundancy_rate']:<6.1f}")
            else:
                print(f"{module:<25} {model:<8} {'N/A':<6} {'N/A':<6} {'N/A':<5} {'N/A':<6} {'N/A':<6}")
        
        # Calculate module redundancy rate
        module_rate = (module_total_redundant / module_total_assertions * 100) if module_total_assertions > 0 else 0
        module_stats[module] = {
            'total_assertions': module_total_assertions,
            'total_redundant': module_total_redundant,
            'redundancy_rate': module_rate
        }
        
        print(f"{'':<25} {'TOTAL':<8} {module_total_assertions:<6} {'':<6} {'':<5} {module_total_redundant:<6} {module_rate:<6.1f}")
        print("-" * 100)
    
    # Summary by model
    print("\nSUMMARY BY MODEL:")
    print("-" * 60)
    print(f"{'Model':<8} {'Experiments':<12} {'Total Assertions':<16} {'Redundant':<10} {'Rate%':<6}")
    print("-" * 60)
    
    for model in models:
        if model in model_stats:
            stats = model_stats[model]
            rate = (stats['total_redundant'] / stats['total_assertions'] * 100) if stats['total_assertions'] > 0 else 0
            print(f"{model:<8} {stats['experiments']:<12} {stats['total_assertions']:<16} {stats['total_redundant']:<10} {rate:<6.1f}")
    
    # Find highest and lowest redundancy rates
    print("\nHIGHEST REDUNDANCY RATES:")
    print("-" * 60)
    
    all_results = []
    for module in results:
        for model in results[module]:
            data = results[module][model]
            if data['has_duplicates']:
                all_results.append((module, model, data['redundancy_rate'], data['redundant_assertions']))
    
    # Sort by redundancy rate (descending)
    all_results.sort(key=lambda x: x[2], reverse=True)
    
    print(f"{'Module':<25} {'Model':<8} {'Rate%':<6} {'Redundant':<10}")
    for module, model, rate, redundant in all_results[:10]:  # Top 10
        print(f"{module:<25} {model:<8} {rate:<6.1f} {redundant:<10}")
    
    print("\nMODULES WITH NO DUPLICATES:")
    print("-" * 40)
    no_duplicates = []
    for module in results:
        for model in results[module]:
            data = results[module][model]
            if not data['has_duplicates']:
                no_duplicates.append((module, model))
    
    for module, model in sorted(no_duplicates):
        print(f"{module:<25} {model}")
    
    print(f"\nTotal modules with no duplicates: {len(no_duplicates)}")

if __name__ == "__main__":
    generate_summary_report() 