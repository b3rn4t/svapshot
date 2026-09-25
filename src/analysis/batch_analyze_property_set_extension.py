#!/usr/bin/env python3
"""
Batch Property Set Extension Analysis Tool

This script analyzes Property Set Extension loops across multiple agent log files,
supporting directory scanning, timestamp filtering, and batch processing.
It generates comparative plots showing property growth patterns across different experiments.

Author: AI Assistant
"""

import argparse
import sys
from pathlib import Path
from typing import List, Dict, Optional
import glob
from analyze_property_set_extension import PropertySetExtensionAnalyzer

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

class BatchPropertySetExtensionAnalyzer(PropertySetExtensionAnalyzer):
    """Batch analyzer for Property Set Extension across multiple experiments."""
    
    def __init__(self):
        super().__init__()
        self.batch_results = {}
    
    def analyze_directory(self, directory_path: str, pattern: str = "*.log", recursive: bool = False) -> List[str]:
        """Find and analyze log files in a directory."""
        directory = Path(directory_path)
        if not directory.exists():
            print(f"Directory not found: {directory_path}")
            return []
        
        # Find log files
        if recursive:
            log_files = list(directory.rglob(pattern))
        else:
            log_files = list(directory.glob(pattern))
        
        # Filter for agent log files (look for 'agent_log' in filename)
        agent_logs = [f for f in log_files if 'agent_log' in f.name]
        
        if not agent_logs:
            print(f"No agent log files found in {directory_path}")
            return []
        
        print(f"Found {len(agent_logs)} agent log files in {directory_path}")
        
        # Analyze files
        file_paths = [str(f) for f in agent_logs]
        self.analyze_files(file_paths)
        
        return file_paths
    
    def filter_by_timestamp(self, directory_path: str, timestamp: str, recursive: bool = False) -> List[str]:
        """Find experiment directories by timestamp and analyze their log files."""
        base_dir = Path(directory_path)
        if not base_dir.exists():
            print(f"Directory not found: {directory_path}")
            return []
        
        # Find experiment directories matching the timestamp pattern
        pattern = f"*{timestamp}*"
        
        if recursive:
            exp_dirs = [d for d in base_dir.rglob("*") if d.is_dir() and timestamp in d.name]
        else:
            exp_dirs = [d for d in base_dir.glob(pattern) if d.is_dir()]
        
        if not exp_dirs:
            print(f"No experiment directories found with timestamp {timestamp}")
            return []
        
        print(f"Found {len(exp_dirs)} experiment directories with timestamp {timestamp}")
        
        # Collect all agent log files from these directories
        all_log_files = []
        for exp_dir in exp_dirs:
            log_files = list(exp_dir.rglob("agent_log_*.log"))
            all_log_files.extend(log_files)
            if log_files:
                print(f"  {exp_dir.name}: {len(log_files)} log files")
        
        if not all_log_files:
            print("No agent log files found in experiment directories")
            return []
        
        # Analyze files
        file_paths = [str(f) for f in all_log_files]
        self.analyze_files(file_paths)
        
        return file_paths
    
    def generate_comparative_extension_plot(self, output_dir: str = "charts", 
                                          group_by: str = "llm") -> None:
        """Generate comparative property extension plots grouped by LLM or module."""
        if not MATPLOTLIB_AVAILABLE:
            print("Matplotlib not available. Cannot generate extension plots.")
            return
        
        if not self.experiments:
            print("No experiment data to plot.")
            return
        
        # Create output directory
        Path(output_dir).mkdir(exist_ok=True)
        
        # Group experiments
        groups = self._group_experiments(group_by)
        
        if len(groups) > 1:
            # Create subplots for different groups
            fig, axes = plt.subplots(len(groups), 1, figsize=(12, 4 * len(groups)))
            if len(groups) == 1:
                axes = [axes]
        else:
            fig, ax = plt.subplots(1, 1, figsize=(12, 6))
            axes = [ax]
        
        # Fixed colors for consistent visualization
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
        
        # Module colors for when grouping by LLM
        module_colors = {
            'ptw': '#3498db',             # Blue
            'ptw_arb': '#2ecc71',         # Green
            'pseudoLRU': '#e74c3c',       # Red
            'hl_mul_unit': '#f39c12',     # Orange
            'hl_div_unit': '#9b59b6',     # Purple
            'hl_div_4bits': '#1abc9c',    # Teal
            'i2c_master_top': '#e67e22',  # Dark Orange
            'i2c_master_byte_ctrl': '#34495e',  # Dark Blue
        }
        
        # Fallback colors
        fallback_colors = list(mcolors.TABLEAU_COLORS.values())
        
        for group_idx, (group_name, experiments) in enumerate(groups.items()):
            ax = axes[group_idx] if len(groups) > 1 else axes[0]
            
            # Plot each experiment in the group
            for exp_idx, (exp_name, exp_data) in enumerate(experiments.items()):
                if not exp_data.iterations:
                    continue
                
                # Prepare data
                iterations = [0] + [iter.iteration_number for iter in exp_data.iterations]
                initial_props = (exp_data.iterations[0].cumulative_properties - 
                               exp_data.iterations[0].properties_added if exp_data.iterations else 0)
                cumulative_props = [initial_props] + [iter.cumulative_properties for iter in exp_data.iterations]
                
                # Create legend label and get consistent color
                if group_by == "llm":
                    legend_label = exp_data.rtl_module
                    # Use module color when grouping by LLM
                    color = module_colors.get(exp_data.rtl_module,
                                            fallback_colors[exp_idx % len(fallback_colors)])
                else:
                    # Format LLM names for better readability
                    if exp_data.llm_type == 'gpt_41_mini':
                        legend_label = 'GPT-4.1-MINI'
                    elif exp_data.llm_type == 'llama_33_70b':
                        legend_label = 'LLAMA-3.3-70B'
                    elif exp_data.llm_type == 'llama_405b':
                        legend_label = 'LLAMA-405B'
                    else:
                        legend_label = exp_data.llm_type.replace('_', '-').upper()
                    
                    # Use LLM color when grouping by module
                    color = llm_colors.get(exp_data.llm_type,
                                         llm_colors.get(exp_data.llm_type.split('_')[-1],
                                                       fallback_colors[exp_idx % len(fallback_colors)]))
                
                # Plot line
                ax.plot(iterations, cumulative_props, 
                       marker='o', linewidth=2, markersize=6,
                       color=color, label=legend_label)
                
                # Add value annotations for property additions
                for i, (x, y) in enumerate(zip(iterations[1:], cumulative_props[1:])):
                    added = exp_data.iterations[i].properties_added
                    ax.annotate(f'+{added}', (x, y), 
                              textcoords="offset points", xytext=(0,6), 
                              ha='center', fontsize=6, color=color, fontweight='bold')
            
            # Customize subplot
            group_title = f"Property Extension Growth - {group_name.upper()}"
            ax.set_title(group_title, fontsize=10, fontweight='bold', pad=8)
            ax.set_xlabel('Extension Iteration', fontsize=9, fontweight='bold')
            ax.set_ylabel('Cumulative Properties', fontsize=9, fontweight='bold')
            
            # Set integer ticks for x-axis
            max_iterations = max(len(exp_data.iterations) for exp_data in experiments.values())
            ax.set_xticks(range(max_iterations + 1))
            
            # Add grid and legend
            ax.grid(True, alpha=0.3, linestyle='--')
            ax.legend(bbox_to_anchor=(1.05, 1), loc='upper left', fontsize=7)
        
        # Main title
        if len(groups) > 1:
            fig.suptitle('Property Set Extension Analysis - Comparative View', 
                        fontsize=12, fontweight='bold', y=0.98)
        
        # Adjust layout with space for main title
        if len(groups) > 1:
            plt.tight_layout(rect=[0, 0, 1, 0.96])  # Leave space at top for main title
        else:
            plt.tight_layout()
        
        # Save plot
        suffix = "by_llm" if group_by == "llm" else "by_module"
        output_file = f"{output_dir}/property_extension_comparative_{suffix}.png"
        plt.savefig(output_file, dpi=300, bbox_inches='tight', facecolor='white')
        print(f"\n📊 Comparative extension plot saved to: {output_file}")
        
        # Only attempt to show plot if running in interactive mode
        if hasattr(plt.get_backend(), 'show') and plt.get_backend() != 'Agg':
            try:
                plt.show()
            except Exception:
                pass  # Non-interactive environment or no display
        
        plt.close()
    
    def _group_experiments(self, group_by: str) -> Dict:
        """Group experiments by LLM type or module."""
        groups = {}
        
        for exp_name, exp_data in self.experiments.items():
            if group_by == "llm":
                key = exp_data.llm_type
            else:  # group by module
                # Use the full module name (already correctly extracted)
                # Remove timestamp part if it exists (for cleaner grouping)
                module_parts = exp_data.rtl_module.split('_')
                # Remove timestamp-like parts (format: YYYYMMDD_HHMMSS)
                clean_parts = []
                for part in module_parts:
                    # Skip timestamp parts (8 digits followed by 6 digits pattern)
                    if len(part) == 8 and part.isdigit():
                        break
                    clean_parts.append(part)
                key = '_'.join(clean_parts) if clean_parts else exp_data.rtl_module
            
            if key not in groups:
                groups[key] = {}
            groups[key][exp_name] = exp_data
        
        return groups
    
    def print_comparative_summary(self, results: Dict):
        """Print a detailed comparative summary."""
        print("="*80)
        print("BATCH PROPERTY SET EXTENSION ANALYSIS")
        print("="*80)
        print(f"Files analyzed: {results['files_analyzed']}")
        print(f"Successful analyses: {results['successful_analyses']}")
        print(f"Experiments found: {len(results['experiments'])}")
        print()
        
        if not results['experiments']:
            print("No extension data found.")
            return
        
        # Summary table
        print("EXPERIMENT OVERVIEW:")
        print("-" * 100)
        print(f"{'Experiment':<40} {'Module':<20} {'LLM':<10} {'Iterations':<12} {'Added':<8} {'Max Props':<10}")
        print("-" * 100)
        
        for exp_name, exp_data in results['experiments'].items():
            module = exp_data['rtl_module'][:19] + "..." if len(exp_data['rtl_module']) > 19 else exp_data['rtl_module']
            print(f"{exp_name:<40} {module:<20} {exp_data['llm_type']:<10} "
                  f"{exp_data['total_iterations']:<12} {exp_data['total_properties_added']:<8} "
                  f"{exp_data['max_cumulative_properties']:<10}")
        
        # Group analysis
        self._print_group_analysis(results)
        
        # Overall statistics
        self._print_overall_statistics(results)
    
    def _print_group_analysis(self, results: Dict):
        """Print analysis grouped by different criteria."""
        print("\n" + "="*80)
        print("GROUP ANALYSIS")
        print("="*80)
        
        # Group by LLM
        llm_groups = {}
        module_groups = {}
        
        for exp_name, exp_data in results['experiments'].items():
            llm = exp_data['llm_type']
            # Use the same module cleaning logic as _group_experiments
            module_parts = exp_data['rtl_module'].split('_')
            clean_parts = []
            for part in module_parts:
                # Skip timestamp parts (8 digits)
                if len(part) == 8 and part.isdigit():
                    break
                clean_parts.append(part)
            module = '_'.join(clean_parts) if clean_parts else exp_data['rtl_module']
            
            if llm not in llm_groups:
                llm_groups[llm] = []
            llm_groups[llm].append(exp_data)
            
            if module not in module_groups:
                module_groups[module] = []
            module_groups[module].append(exp_data)
        
        # LLM Analysis
        print("\n📊 BY LLM TYPE:")
        print("-" * 50)
        for llm, experiments in llm_groups.items():
            avg_iterations = sum(exp['total_iterations'] for exp in experiments) / len(experiments)
            avg_properties = sum(exp['total_properties_added'] for exp in experiments) / len(experiments)
            avg_max_props = sum(exp['max_cumulative_properties'] for exp in experiments) / len(experiments)
            
            print(f"  {llm.upper()}:")
            print(f"    Experiments: {len(experiments)}")
            print(f"    Avg iterations: {avg_iterations:.1f}")
            print(f"    Avg properties added: {avg_properties:.1f}")
            print(f"    Avg max properties: {avg_max_props:.1f}")
        
        # Module Analysis  
        print("\n🧩 BY RTL MODULE:")
        print("-" * 50)
        for module, experiments in module_groups.items():
            avg_iterations = sum(exp['total_iterations'] for exp in experiments) / len(experiments)
            avg_properties = sum(exp['total_properties_added'] for exp in experiments) / len(experiments)
            avg_max_props = sum(exp['max_cumulative_properties'] for exp in experiments) / len(experiments)
            
            print(f"  {module.upper()}:")
            print(f"    Experiments: {len(experiments)}")
            print(f"    Avg iterations: {avg_iterations:.1f}")
            print(f"    Avg properties added: {avg_properties:.1f}")
            print(f"    Avg max properties: {avg_max_props:.1f}")
    
    def _print_overall_statistics(self, results: Dict):
        """Print overall statistics."""
        if not results['experiments']:
            return
        
        all_iterations = [exp['total_iterations'] for exp in results['experiments'].values()]
        all_properties = [exp['total_properties_added'] for exp in results['experiments'].values()]
        all_max_props = [exp['max_cumulative_properties'] for exp in results['experiments'].values()]
        
        print("\n" + "="*80)
        print("OVERALL STATISTICS")
        print("="*80)
        print(f"Total experiments analyzed: {len(results['experiments'])}")
        print(f"Extension iterations - Avg: {sum(all_iterations) / len(all_iterations):.2f}, "
              f"Range: {min(all_iterations)}-{max(all_iterations)}")
        print(f"Properties added - Avg: {sum(all_properties) / len(all_properties):.2f}, "
              f"Range: {min(all_properties)}-{max(all_properties)}")
        print(f"Max properties reached - Avg: {sum(all_max_props) / len(all_max_props):.2f}, "
              f"Range: {min(all_max_props)}-{max(all_max_props)}")

def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(
        description="Batch analyze Property Set Extension loops across multiple experiments",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze all log files in a directory
  python3 src/analysis/batch_analyze_property_set_extension.py --directory svapshot_v1_results/
  
  # Analyze experiments from a specific timestamp
  python3 src/analysis/batch_analyze_property_set_extension.py --directory svapshot_v1_results/ --timestamp 20250823_014948
  
  # Analyze specific files with comparative plots
  python3 src/analysis/batch_analyze_property_set_extension.py --files file1.log file2.log --plot
  
  # Recursive directory search with grouping by LLM
  python3 src/analysis/batch_analyze_property_set_extension.py --directory svapshot_v1_results/ --recursive --plot --group-by llm
        """
    )
    
    # Input options
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument('--directory', '-d', help='Directory to search for log files')
    input_group.add_argument('--files', '-f', nargs='+', help='Specific log files to analyze')
    
    # Filtering options
    parser.add_argument('--timestamp', '-t', help='Filter by experiment timestamp (e.g., 20250823_014948)')
    parser.add_argument('--recursive', '-r', action='store_true', help='Search directories recursively')
    parser.add_argument('--pattern', '-p', default='*.log', help='File pattern to match (default: *.log)')
    
    # Output options
    parser.add_argument('--output', '-o', help='Save results to JSON file')
    parser.add_argument('--verbose', '-v', action='store_true', help='Show detailed analysis')
    
    # Plot options
    parser.add_argument('--plot', action='store_true', 
                       help='Generate comparative extension plots (requires matplotlib)')
    parser.add_argument('--group-by', choices=['llm', 'module'], default='llm',
                       help='Group plots by LLM type or RTL module (default: llm)')
    parser.add_argument('--chart-dir', default='charts', 
                       help='Directory to save charts (default: charts)')
    
    args = parser.parse_args()
    
    # Initialize analyzer
    analyzer = BatchPropertySetExtensionAnalyzer()
    
    # Collect files to analyze
    if args.files:
        print(f"Analyzing {len(args.files)} specified log files...")
        file_paths = args.files
        results = analyzer.analyze_files(file_paths)
    elif args.timestamp:
        print(f"Searching for experiments with timestamp {args.timestamp}...")
        file_paths = analyzer.filter_by_timestamp(args.directory, args.timestamp, args.recursive)
        results = analyzer.batch_results if hasattr(analyzer, 'batch_results') else analyzer.analyze_files(file_paths)
    else:
        print(f"Scanning directory: {args.directory}")
        file_paths = analyzer.analyze_directory(args.directory, args.pattern, args.recursive)
        results = analyzer.batch_results if hasattr(analyzer, 'batch_results') else analyzer.analyze_files(file_paths)
    
    # Get current results
    results = analyzer.analyze_files(file_paths) if file_paths else {'files_analyzed': 0, 'successful_analyses': 0, 'experiments': {}}
    
    # Print results
    if args.verbose:
        analyzer.print_comparative_summary(results)
    else:
        print(f"✅ Batch analysis complete: {results['successful_analyses']}/{results['files_analyzed']} files processed")
        print(f"Found extension data in {len(results['experiments'])} experiments")
    
    # Generate plots if requested
    if args.plot and results['experiments']:
        analyzer.generate_comparative_extension_plot(args.chart_dir, args.group_by)
    
    # Save results if requested
    if args.output:
        import json
        with open(args.output, 'w') as f:
            json.dump(results, f, indent=2)
        print(f"📄 Batch results saved to: {args.output}")

if __name__ == "__main__":
    main() 