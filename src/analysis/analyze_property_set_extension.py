#!/usr/bin/env python3
"""
Property Set Extension Loop Analysis Tool

This script analyzes the Property Set Extension phase in agent log files,
tracking how the property sets grow through multiple extension iterations.
It generates accumulative plots showing the number of properties added
in each iteration across multiple experiments.

Author: AI Assistant
"""

import argparse
import re
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from collections import defaultdict
from dataclasses import dataclass

# Check for matplotlib availability
try:
    import warnings
    # Suppress common matplotlib warnings
    warnings.filterwarnings('ignore', category=UserWarning, module='matplotlib')
    warnings.filterwarnings('ignore', message='.*GUI is implemented.*')
    warnings.filterwarnings('ignore', message='.*backend.*')
    
    import matplotlib
    # Use non-interactive backend to avoid GUI warnings
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

@dataclass
class ExtensionIteration:
    """Represents a single property set extension iteration."""
    iteration_number: int
    properties_added: int
    cumulative_properties: int
    added_assertions: List[str]

@dataclass
class ExperimentExtensionData:
    """Contains all extension data for a single experiment."""
    experiment_name: str
    log_file_path: str
    rtl_module: str
    llm_type: str
    iterations: List[ExtensionIteration]
    total_properties_added: int
    
    @property
    def max_cumulative_properties(self) -> int:
        """Returns the maximum cumulative properties reached."""
        return max((iter.cumulative_properties for iter in self.iterations), default=0)

class PropertySetExtensionAnalyzer:
    """Analyzer for Property Set Extension loops."""
    
    def __init__(self):
        self.experiments: Dict[str, ExperimentExtensionData] = {}
        self.patterns = {
            'new_assertions_start': re.compile(r'=== NEW ASSERTIONS ADDED \(After Syntax Correction\) ==='),
            'added_assertion': re.compile(r'Added Assertion (\d+): (.+)'),
            'properties_added_summary': re.compile(r'(\d+) NEW PROPERTIES added to the set'),
            'extension_iteration_end': re.compile(r'FINISHED EXTENSION LOOP ITERATION'),
            'found_properties': re.compile(r'Found (\d+) syntactically correct new properties'),
            'iteration_start': re.compile(r'Requesting LLM for new assertions\.\.\.'),
            'initial_properties': re.compile(r'Initial correct assertions for extension iteration (\d+):')
        }
    
    def parse_log_file(self, log_file_path: str) -> Optional[ExperimentExtensionData]:
        """Parse a single log file for property set extension data."""
        log_path = Path(log_file_path)
        
        # Extract experiment information from path
        experiment_name = self._extract_experiment_name(log_path)
        rtl_module, llm_type = self._extract_experiment_details(log_path)
        
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            print(f"Error reading {log_path}: {e}")
            return None
        
        # Parse extension iterations
        iterations = self._parse_extension_iterations(content)
        
        if not iterations:
            print(f"No property set extension iterations found in {log_path}")
            return None
        
        total_added = sum(iter.properties_added for iter in iterations)
        
        experiment_data = ExperimentExtensionData(
            experiment_name=experiment_name,
            log_file_path=str(log_path),
            rtl_module=rtl_module,
            llm_type=llm_type,
            iterations=iterations,
            total_properties_added=total_added
        )
        
        return experiment_data
    
    def _extract_experiment_name(self, log_path: Path) -> str:
        """Extract experiment name from log file path."""
        # Look for experiment directory pattern like ft_<module>_<timestamp>_<llm>/
        parts = log_path.parts
        for part in reversed(parts):
            if part.startswith('ft_') and '_' in part:
                return part
        return log_path.stem
    
    def _extract_experiment_details(self, log_path: Path) -> Tuple[str, str]:
        """Extract RTL module and LLM type from experiment name."""
        experiment_name = self._extract_experiment_name(log_path)
        
        # Parse pattern: ft_<module>_<timestamp>_<llm_type>
        # Where timestamp is YYYYMMDD_HHMMSS (2 parts when split by _)
        parts = experiment_name.split('_')
        if len(parts) >= 4:
            # Find timestamp pattern (8 digits followed by 6 digits)
            timestamp_start_idx = None
            for i, part in enumerate(parts):
                if len(part) == 8 and part.isdigit():
                    # Check if next part is 6 digits (time part)
                    if i + 1 < len(parts) and len(parts[i + 1]) == 6 and parts[i + 1].isdigit():
                        timestamp_start_idx = i
                        break
            
            if timestamp_start_idx is not None:
                # Module is everything between 'ft_' and timestamp
                module = '_'.join(parts[1:timestamp_start_idx])
                # LLM type is everything after timestamp
                llm_type = '_'.join(parts[timestamp_start_idx + 2:])
                return module, llm_type
            else:
                # Fallback: assume last part is LLM type
                module = '_'.join(parts[1:-1])
                llm_type = parts[-1]
                return module, llm_type
        
        return "unknown_module", "unknown_llm"
    
    def _parse_extension_iterations(self, content: str) -> List[ExtensionIteration]:
        """Parse extension iterations from log content."""
        lines = content.split('\n')
        iterations = []
        current_iteration = None
        iteration_number = 0
        cumulative_count = 0
        
        # Track initial properties count
        initial_properties_found = False
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            # Look for initial properties count to establish baseline
            if not initial_properties_found and 'Initial correct assertions for extension iteration' in line:
                # Count assertions following this line until we hit a blank line or new section
                initial_count = 0
                j = i + 1
                while j < len(lines) and lines[j].strip() and not lines[j].startswith('==='):
                    if lines[j].strip().startswith('as__'):
                        initial_count += 1
                    j += 1
                cumulative_count = initial_count
                initial_properties_found = True
            
            # Check for start of new assertion addition
            elif self.patterns['new_assertions_start'].search(line):
                iteration_number += 1
                added_assertions = []
                properties_count = 0
                
                # Parse added assertions
                j = i + 1
                while j < len(lines):
                    assert_line = lines[j].strip()
                    
                    # Check for added assertion
                    assert_match = self.patterns['added_assertion'].search(assert_line)
                    if assert_match:
                        assertion_num = int(assert_match.group(1))
                        assertion_text = assert_match.group(2)
                        added_assertions.append(assertion_text)
                        properties_count += 1
                    
                    # Check for end of assertions section
                    elif assert_line.startswith('======================================================================'):
                        break
                    
                    j += 1
                
                # Look for properties added summary
                while j < len(lines):
                    summary_line = lines[j].strip()
                    
                    # Check for properties added count
                    props_match = self.patterns['properties_added_summary'].search(summary_line)
                    if props_match:
                        properties_count = int(props_match.group(1))
                        break
                    
                    # Check for found properties count as backup
                    found_match = self.patterns['found_properties'].search(summary_line)
                    if found_match:
                        properties_count = int(found_match.group(1))
                    
                    j += 1
                
                # Update cumulative count
                cumulative_count += properties_count
                
                # Create iteration object
                iteration = ExtensionIteration(
                    iteration_number=iteration_number,
                    properties_added=properties_count,
                    cumulative_properties=cumulative_count,
                    added_assertions=added_assertions
                )
                
                iterations.append(iteration)
                i = j
            
            else:
                i += 1
        
        return iterations
    
    def analyze_file(self, log_file_path: str) -> bool:
        """Analyze a single log file and add to results."""
        experiment_data = self.parse_log_file(log_file_path)
        if experiment_data:
            self.experiments[experiment_data.experiment_name] = experiment_data
            return True
        return False
    
    def analyze_files(self, file_list: List[str]) -> Dict:
        """Analyze multiple log files."""
        results = {}
        successful_files = 0
        
        for file_path in file_list:
            if self.analyze_file(file_path):
                successful_files += 1
        
        # Generate analysis results
        results = {
            'files_analyzed': len(file_list),
            'successful_analyses': successful_files,
            'experiments': {}
        }
        
        for exp_name, exp_data in self.experiments.items():
            results['experiments'][exp_name] = {
                'rtl_module': exp_data.rtl_module,
                'llm_type': exp_data.llm_type,
                'total_iterations': len(exp_data.iterations),
                'total_properties_added': exp_data.total_properties_added,
                'max_cumulative_properties': exp_data.max_cumulative_properties,
                'iterations': [
                    {
                        'iteration': iter.iteration_number,
                        'properties_added': iter.properties_added,
                        'cumulative_properties': iter.cumulative_properties
                    } for iter in exp_data.iterations
                ]
            }
        
        return results
    
    def generate_extension_plot(self, output_dir: str = "charts"):
        """Generate cumulative property extension plots."""
        if not MATPLOTLIB_AVAILABLE:
            print("Matplotlib not available. Cannot generate extension plots.")
            return
        
        if not self.experiments:
            print("No experiment data to plot.")
            return
        
        # Create output directory
        Path(output_dir).mkdir(exist_ok=True)
        
        # Create figure
        plt.figure(figsize=(10, 6))
        
        # Fixed colors for LLM types for consistency across runs
        llm_colors = {
            'gpt_41_mini': '#e74c3c',     # Red
            'mini': '#e74c3c',            # Red (alternative naming)
            'llama_33_70b': '#3498db',    # Blue  
            '70b': '#3498db',             # Blue (alternative naming)
            'llama_405b': '#2ecc71',      # Green
            '405b': '#2ecc71',            # Green (alternative naming)
            'gpt_4': '#f39c12',           # Orange (if GPT-4 full appears)
            'claude': '#9b59b6',          # Purple (if Claude appears)
        }
        
        # Fallback colors if we run out of predefined ones
        fallback_colors = list(mcolors.TABLEAU_COLORS.values())
        
        # Plot each experiment
        for idx, (exp_name, exp_data) in enumerate(self.experiments.items()):
            if not exp_data.iterations:
                continue
            
            # Prepare data for plotting
            iterations = [0] + [iter.iteration_number for iter in exp_data.iterations]
            cumulative_props = [exp_data.iterations[0].cumulative_properties - exp_data.iterations[0].properties_added if exp_data.iterations else 0]
            cumulative_props.extend([iter.cumulative_properties for iter in exp_data.iterations])
            
            # Create legend label with formatted LLM name
            if exp_data.llm_type == 'gpt_41_mini':
                llm_formatted = 'GPT-4.1-MINI'
            elif exp_data.llm_type == 'llama_33_70b':
                llm_formatted = 'LLAMA-3.3-70B'
            elif exp_data.llm_type == 'llama_405b':
                llm_formatted = 'LLAMA-405B'
            else:
                llm_formatted = exp_data.llm_type.replace('_', '-').upper()
            
            legend_label = f"{exp_data.rtl_module}-{llm_formatted}"
            
            # Get consistent color for this LLM type
            color = llm_colors.get(exp_data.llm_type, 
                                  llm_colors.get(exp_data.llm_type.split('_')[-1], 
                                                fallback_colors[idx % len(fallback_colors)]))
            
            # Plot line
            plt.plot(iterations, cumulative_props, 
                    marker='o', linewidth=2, markersize=6,
                    color=color, label=legend_label)
            
            # Add value annotations
            for i, (x, y) in enumerate(zip(iterations[1:], cumulative_props[1:])):
                added = exp_data.iterations[i].properties_added
                plt.annotate(f'+{added}', (x, y), 
                           textcoords="offset points", xytext=(0,6), 
                           ha='center', fontsize=6, color=color, fontweight='bold')
        
        # Customize plot
        plt.title('Property Set Extension Growth Across Experiments', 
                 fontsize=12, fontweight='bold', pad=12)
        plt.xlabel('Extension Iteration', fontsize=9, fontweight='bold')
        plt.ylabel('Cumulative Number of Properties', fontsize=9, fontweight='bold')
        
        # Set integer ticks for x-axis
        max_iterations = max(len(exp_data.iterations) for exp_data in self.experiments.values())
        plt.xticks(range(max_iterations + 1))
        
        # Add grid
        plt.grid(True, alpha=0.3, linestyle='--')
        
        # Add legend
        plt.legend(bbox_to_anchor=(1.05, 1), loc='upper left', fontsize=7)
        
        # Adjust layout to prevent legend cutoff
        plt.tight_layout()
        
        # Save plot
        output_file = f"{output_dir}/property_set_extension_growth.png"
        plt.savefig(output_file, dpi=300, bbox_inches='tight', facecolor='white')
        print(f"\n📈 Property set extension plot saved to: {output_file}")
        
        # Only attempt to show plot if running in interactive mode
        if hasattr(plt.get_backend(), 'show') and plt.get_backend() != 'Agg':
            try:
                plt.show()
            except Exception:
                pass  # Non-interactive environment or no display
        
        plt.close()
    
    def print_summary(self, results: Dict):
        """Print detailed analysis summary."""
        print("="*80)
        print("PROPERTY SET EXTENSION ANALYSIS")
        print("="*80)
        print(f"Files analyzed: {results['files_analyzed']}")
        print(f"Successful analyses: {results['successful_analyses']}")
        print()
        
        if not results['experiments']:
            print("No extension data found.")
            return
        
        print("EXPERIMENT SUMMARY:")
        print("-" * 80)
        print(f"{'Experiment':<35} {'Module':<15} {'LLM':<12} {'Iterations':<12} {'Total Added':<12} {'Max Props'}")
        print("-" * 80)
        
        for exp_name, exp_data in results['experiments'].items():
            module = exp_data['rtl_module'][:14] + "..." if len(exp_data['rtl_module']) > 14 else exp_data['rtl_module']
            print(f"{exp_name:<35} {module:<15} {exp_data['llm_type']:<12} "
                  f"{exp_data['total_iterations']:<12} {exp_data['total_properties_added']:<12} "
                  f"{exp_data['max_cumulative_properties']}")
        
        print()
        print("DETAILED ITERATION BREAKDOWN:")
        print("="*80)
        
        for exp_name, exp_data in results['experiments'].items():
            print(f"\n🔬 {exp_name}")
            print(f"   RTL Module: {exp_data['rtl_module']}")
            print(f"   LLM Type: {exp_data['llm_type']}")
            print(f"   Total Extension Iterations: {exp_data['total_iterations']}")
            print(f"   Total Properties Added: {exp_data['total_properties_added']}")
            
            if exp_data['iterations']:
                print(f"   Extension Growth Pattern:")
                for iter_data in exp_data['iterations']:
                    print(f"     Iteration {iter_data['iteration']}: "
                          f"+{iter_data['properties_added']} properties "
                          f"(cumulative: {iter_data['cumulative_properties']})")
        
        # Statistics
        if results['experiments']:
            all_iterations = [exp['total_iterations'] for exp in results['experiments'].values()]
            all_properties = [exp['total_properties_added'] for exp in results['experiments'].values()]
            all_max_props = [exp['max_cumulative_properties'] for exp in results['experiments'].values()]
            
            print("\n" + "="*80)
            print("SUMMARY STATISTICS:")
            print("-" * 80)
            print(f"Average extension iterations per experiment: {sum(all_iterations) / len(all_iterations):.2f}")
            print(f"Average properties added per experiment: {sum(all_properties) / len(all_properties):.2f}")
            print(f"Average maximum properties reached: {sum(all_max_props) / len(all_max_props):.2f}")
            print(f"Range of properties added: {min(all_properties)}-{max(all_properties)}")
            print(f"Range of maximum properties: {min(all_max_props)}-{max(all_max_props)}")

def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(
        description="Analyze Property Set Extension loops in agent log files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze a single log file
  python analyze_property_set_extension.py log_file.log
  
  # Analyze multiple files and generate plots
  python analyze_property_set_extension.py file1.log file2.log --plot
  
  # Save results to JSON
  python analyze_property_set_extension.py *.log --output results.json
  
  # Generate verbose summary
  python analyze_property_set_extension.py *.log --verbose
        """
    )
    
    parser.add_argument('log_files', nargs='+', help='Agent log files to analyze')
    parser.add_argument('--output', '-o', help='Save results to JSON file')
    parser.add_argument('--plot', '-p', action='store_true', 
                       help='Generate property extension growth plot (requires matplotlib)')
    parser.add_argument('--chart-dir', default='charts', 
                       help='Directory to save charts (default: charts)')
    parser.add_argument('--verbose', '-v', action='store_true', 
                       help='Show detailed analysis')
    
    args = parser.parse_args()
    
    # Initialize analyzer
    analyzer = PropertySetExtensionAnalyzer()
    
    # Analyze files
    print(f"Analyzing {len(args.log_files)} log files for property set extension data...")
    results = analyzer.analyze_files(args.log_files)
    
    # Print summary
    if args.verbose:
        analyzer.print_summary(results)
    else:
        print(f"✅ Analysis complete: {results['successful_analyses']}/{results['files_analyzed']} files processed")
        print(f"Found extension data in {len(results['experiments'])} experiments")
    
    # Generate plot if requested
    if args.plot:
        analyzer.generate_extension_plot(args.chart_dir)
    
    # Save results if requested
    if args.output:
        with open(args.output, 'w') as f:
            json.dump(results, f, indent=2)
        print(f"📄 Results saved to: {args.output}")

if __name__ == "__main__":
    main() 