#!/usr/bin/env python3
"""
Script to detect duplicate SystemVerilog assertion properties in SVA files.
Analyzes assertion properties (not assertion names) and reports duplicates.
"""

import sys
import re
from collections import defaultdict
from pathlib import Path

def extract_assertion_info(line):
    """
    Extract assertion name and property from a SystemVerilog assertion line.
    
    Args:
        line (str): Line containing an assertion
    
    Returns:
        tuple: (assertion_name, property_logic) or (None, None) if not an assertion
    """
    # Remove leading/trailing whitespace
    clean_line = line.strip()
    
    # Skip empty lines and comments
    if not clean_line or clean_line.startswith('//'):
        return None, None
    
    # Pattern to match SystemVerilog assertions
    # Matches: assert property(property_logic);
    # Also captures assertion names like: as__Name: assert property(property_logic);
    assertion_pattern = r'^(?:(\w+)\s*:\s*)?assert\s+property\s*\((.*)\)\s*;?\s*$'
    
    match = re.match(assertion_pattern, clean_line)
    if match:
        assertion_name = match.group(1) if match.group(1) else "unnamed_assertion"
        property_logic = match.group(2).strip()
        return assertion_name, property_logic
    
    return None, None

def normalize_property(property_logic):
    """
    Normalize property logic for comparison by removing extra whitespace and 
    normalizing parentheses structure for better duplicate detection.
    
    Args:
        property_logic (str): The property logic to normalize
    
    Returns:
        str: Normalized property logic
    """
    if not property_logic:
        return ""
    
    # Remove extra whitespace while preserving structure
    normalized = re.sub(r'\s+', ' ', property_logic.strip())
    
    # Normalize spacing around operators first
    normalized = re.sub(r'\s*\|\->\s*', ' |-> ', normalized)
    normalized = re.sub(r'\s*\|\=>\s*', ' |=> ', normalized)
    normalized = re.sub(r'\s*&&\s*', ' && ', normalized)
    normalized = re.sub(r'\s*\|\|\s*', ' || ', normalized)
    normalized = re.sub(r'\s*==\s*', ' == ', normalized)
    normalized = re.sub(r'\s*!=\s*', ' != ', normalized)
    
    # Simple approach: if we have |-> in the string, try to normalize parentheses
    if ' |-> ' in normalized:
        parts = normalized.split(' |-> ', 1)
        if len(parts) == 2:
            left_part = parts[0].strip()
            right_part = parts[1].strip()
            
            # Remove outer parentheses from left part if they exist
            if left_part.startswith('(') and left_part.endswith(')'):
                # Check if these are the outermost parentheses
                paren_count = 0
                can_remove = True
                for i, char in enumerate(left_part[1:-1]):
                    if char == '(':
                        paren_count += 1
                    elif char == ')':
                        paren_count -= 1
                        if paren_count < 0:
                            can_remove = False
                            break
                if can_remove and paren_count == 0:
                    left_part = left_part[1:-1]
            
            # Remove outer parentheses from right part if they exist
            if right_part.startswith('(') and right_part.endswith(')'):
                # Check if these are the outermost parentheses
                paren_count = 0
                can_remove = True
                for i, char in enumerate(right_part[1:-1]):
                    if char == '(':
                        paren_count += 1
                    elif char == ')':
                        paren_count -= 1
                        if paren_count < 0:
                            can_remove = False
                            break
                if can_remove and paren_count == 0:
                    right_part = right_part[1:-1]
            
            normalized = f"{left_part} |-> {right_part}"
    
    # Handle |=> implications as well
    if ' |=> ' in normalized:
        parts = normalized.split(' |=> ', 1)
        if len(parts) == 2:
            left_part = parts[0].strip()
            right_part = parts[1].strip()
            
            # Remove outer parentheses from left part if they exist
            if left_part.startswith('(') and left_part.endswith(')'):
                # Check if these are the outermost parentheses
                paren_count = 0
                can_remove = True
                for i, char in enumerate(left_part[1:-1]):
                    if char == '(':
                        paren_count += 1
                    elif char == ')':
                        paren_count -= 1
                        if paren_count < 0:
                            can_remove = False
                            break
                if can_remove and paren_count == 0:
                    left_part = left_part[1:-1]
            
            # Remove outer parentheses from right part if they exist
            if right_part.startswith('(') and right_part.endswith(')'):
                # Check if these are the outermost parentheses
                paren_count = 0
                can_remove = True
                for i, char in enumerate(right_part[1:-1]):
                    if char == '(':
                        paren_count += 1
                    elif char == ')':
                        paren_count -= 1
                        if paren_count < 0:
                            can_remove = False
                            break
                if can_remove and paren_count == 0:
                    right_part = right_part[1:-1]
            
            normalized = f"{left_part} |=> {right_part}"
    
    return normalized.strip()

def detect_duplicate_properties(filename):
    """
    Detect duplicate assertion properties in a SystemVerilog file.
    
    Args:
        filename (str): Path to the SVA file to analyze
    
    Returns:
        tuple: (duplicates_dict, total_assertions, total_properties)
    """
    property_occurrences = defaultdict(list)
    total_assertions = 0
    
    try:
        with open(filename, 'r', encoding='utf-8') as file:
            for line_num, line in enumerate(file, 1):
                assertion_name, property_logic = extract_assertion_info(line)
                
                if assertion_name and property_logic:
                    total_assertions += 1
                    normalized_property = normalize_property(property_logic)
                    
                    # Store assertion info: (name, line_number, original_property)
                    assertion_info = {
                        'name': assertion_name,
                        'line': line_num,
                        'original': property_logic
                    }
                    property_occurrences[normalized_property].append(assertion_info)
                    
    except FileNotFoundError:
        print(f"Error: File '{filename}' not found.")
        return {}, 0, 0
    except Exception as e:
        print(f"Error reading file '{filename}': {e}")
        return {}, 0, 0
    
    # Filter to only return properties that appear more than once
    duplicates = {prop: assertions for prop, assertions in property_occurrences.items() 
                  if len(assertions) > 1}
    
    total_unique_properties = len(property_occurrences)
    
    return duplicates, total_assertions, total_unique_properties

def main():
    """Main function to analyze SystemVerilog assertion file for duplicate properties."""
    if len(sys.argv) > 1:
        filename = sys.argv[1]
    else:
        # Default filename for when no argument is provided
        filename = "svapshot_v1_results/ft_hl_mul_unit_20250824_140034_gpt_41_mini/sva/versions/hl_mul_unit_prop_after_extension.sv"
    
    # Check if file exists
    if not Path(filename).exists():
        print(f"Error: File '{filename}' not found.")
        print("Usage: python3 detect_duplicates.py [sva_file]")
        print("If no file is specified, the default file will be analyzed.")
        return
    
    print(f"Analyzing SystemVerilog assertion file: {filename}")
    print("=" * 80)
    
    duplicates, total_assertions, total_unique_properties = detect_duplicate_properties(filename)
    
    if not duplicates:
        print("No duplicate assertion properties found.")
        print(f"\nSummary:")
        print(f"  Total assertions analyzed: {total_assertions}")
        print(f"  Total unique properties: {total_unique_properties}")
        return
    
    print(f"Found {len(duplicates)} property patterns that appear in multiple assertions:\n")
    
    # Sort by first occurrence line number for better readability
    sorted_duplicates = sorted(duplicates.items(), 
                              key=lambda x: min(assertion['line'] for assertion in x[1]))
    
    duplicate_count = 0
    for property_logic, assertions in sorted_duplicates:
        duplicate_count += len(assertions)
        
        print(f"Property appears in {len(assertions)} assertions:")
        print(f"  Property: {property_logic}")
        print(f"  Assertions:")
        
        # Sort assertions by line number
        sorted_assertions = sorted(assertions, key=lambda x: x['line'])
        for assertion in sorted_assertions:
            print(f"    - {assertion['name']} (line {assertion['line']})")
        print()
    
    # Summary statistics
    redundant_assertions = duplicate_count - len(duplicates)
    
    print("=" * 80)
    print(f"Summary:")
    print(f"  Total assertions analyzed: {total_assertions}")
    print(f"  Total unique properties: {total_unique_properties}")
    print(f"  Duplicate property patterns: {len(duplicates)}")
    print(f"  Total duplicate assertion instances: {duplicate_count}")
    print(f"  Redundant assertions (could be removed): {redundant_assertions}")
    
    if redundant_assertions > 0:
        percentage = (redundant_assertions / total_assertions) * 100
        print(f"  Redundancy rate: {percentage:.1f}%")
    
    print(f"\nDuplicate assertions that could be removed:")
    for property_logic, assertions in sorted_duplicates:
        sorted_assertions = sorted(assertions, key=lambda x: x['line'])
        # Keep the first one, suggest removing the rest
        for assertion in sorted_assertions[1:]:
            print(f"  - {assertion['name']} (line {assertion['line']}) - duplicate of {sorted_assertions[0]['name']} (line {sorted_assertions[0]['line']})")

if __name__ == "__main__":
    main() 