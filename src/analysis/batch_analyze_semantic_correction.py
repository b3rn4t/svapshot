#!/usr/bin/env python3
"""
Batch analysis script for Semantic Correction Loop analysis across multiple log files.

This script processes multiple agent log files and generates comparative statistics
and summary reports for the semantic correction process.
"""

import os
import re
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple
import statistics
from analyze_semantic_correction import SemanticCorrectionAnalyzer

# Try to import matplotlib, but make it optional
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
    import matplotlib.patches as mpatches
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False


class BatchSemanticCorrectionAnalyzer:
    def __init__(self):
        self.results = {}  # filename -> analysis results
        
    def analyze_directory(self, directory_path: str, pattern: str = "*.log"):
        """Analyze all log files in a directory matching the pattern."""
        directory = Path(directory_path)
        
        if not directory.exists():
            print(f"Error: Directory '{directory_path}' not found.")
            return
        
        log_files = list(directory.rglob(pattern))
        
        if not log_files:
            print(f"No log files found matching pattern '{pattern}' in '{directory_path}'")
            return
        
        print(f"Found {len(log_files)} log files to analyze...")
        
        for log_file in log_files:
            print(f"Analyzing: {log_file}")
            try:
                analyzer = SemanticCorrectionAnalyzer(str(log_file))
                result = analyzer.analyze()
                self.results[str(log_file)] = result
            except Exception as e:
                print(f"Error analyzing {log_file}: {e}")
    
    def analyze_files(self, file_list: List[str]):
        """Analyze a specific list of log files."""
        for log_file in file_list:
            if not Path(log_file).exists():
                print(f"Warning: File '{log_file}' not found. Skipping.")
                continue
                
            print(f"Analyzing: {log_file}")
            try:
                analyzer = SemanticCorrectionAnalyzer(log_file)
                result = analyzer.analyze()
                self.results[log_file] = result
            except Exception as e:
                print(f"Error analyzing {log_file}: {e}")
    
    def filter_by_timestamp(self, directory_path: str, timestamp: str, recursive: bool = False):
        """Find and analyze log files matching a specific timestamp pattern."""
        directory = Path(directory_path)
        
        if not directory.exists():
            print(f"Error: Directory '{directory_path}' not found.")
            return
        
        # Search for experiment directories with the timestamp
        if recursive:
            experiment_dirs = list(directory.rglob(f"*{timestamp}*"))
        else:
            experiment_dirs = [d for d in directory.iterdir() if d.is_dir() and timestamp in d.name]
        
        if not experiment_dirs:
            print(f"No experiment directories found with timestamp '{timestamp}'")
            return
        
        print(f"Found {len(experiment_dirs)} experiment directories with timestamp '{timestamp}':")
        for exp_dir in experiment_dirs:
            print(f"  - {exp_dir}")
        
        # Find log files in these directories
        log_files = []
        for exp_dir in experiment_dirs:
            logs = list(exp_dir.rglob("*.log"))
            log_files.extend(logs)
        
        if not log_files:
            print(f"No log files found in experiment directories with timestamp '{timestamp}'")
            return
        
        print(f"Found {len(log_files)} log files to analyze...")
        
        # Analyze each log file
        for log_file in log_files:
            print(f"Analyzing: {log_file}")
            try:
                analyzer = SemanticCorrectionAnalyzer(str(log_file))
                result = analyzer.analyze()
                self.results[str(log_file)] = result
            except Exception as e:
                print(f"Error analyzing {log_file}: {e}")
    
    def filter_by_llm_model(self, directory_path: str, llm_model: str, recursive: bool = False):
        """Find and analyze log files matching a specific LLM model pattern."""
        directory = Path(directory_path)
        
        if not directory.exists():
            print(f"Error: Directory '{directory_path}' not found.")
            return
        
        # Normalize the LLM model name for matching
        normalized_llm = llm_model.lower().replace('-', '_').replace('/', '_').replace('.', '_')
        
        # Common LLM model patterns and their normalized forms
        llm_patterns = {
            'gpt_41_mini': ['gpt_4_1_mini', 'gpt41', 'gpt_41_mini'],
            'gpt_4_1_mini': ['gpt_41_mini', 'gpt41', 'gpt_4_1_mini'],
            'gpt41': ['gpt_41_mini', 'gpt_4_1_mini', 'gpt41'],
            'llama_33_70b': ['llama_3_3_70b', 'llama33', 'llama_33_70b'],
            'llama_405b': ['llama_3_1_405b', 'llama405', 'llama_405b'],
            'llama_70b': ['llama_3_1_70b', 'llama70', 'llama_70b'],
            'o4_mini': ['o4mini', 'o4_mini'],
            'o4mini': ['o4_mini', 'o4mini'],
            'o3_mini': ['o3mini', 'o3_mini'],
            'o3mini': ['o3_mini', 'o3mini'],
            'o3': ['o3'],
            'claude': ['claude', 'anthropic'],
            'deepseek': ['deepseek'],
            'nemotron': ['nemotron']
        }
        
        # Get all possible patterns for the given LLM model
        patterns_to_match = [normalized_llm]
        if normalized_llm in llm_patterns:
            patterns_to_match.extend(llm_patterns[normalized_llm])
        
        # Search for experiment directories with the LLM model pattern
        experiment_dirs = []
        if recursive:
            all_dirs = list(directory.rglob("ft_*"))
        else:
            all_dirs = [d for d in directory.iterdir() if d.is_dir() and d.name.startswith('ft_')]
        
        for exp_dir in all_dirs:
            dir_name_lower = exp_dir.name.lower()
            for pattern in patterns_to_match:
                if pattern in dir_name_lower:
                    experiment_dirs.append(exp_dir)
                    break
        
        if not experiment_dirs:
            print(f"No experiment directories found with LLM model '{llm_model}' (searched patterns: {patterns_to_match})")
            return
        
        print(f"Found {len(experiment_dirs)} experiment directories with LLM model '{llm_model}':")
        for exp_dir in experiment_dirs:
            print(f"  - {exp_dir}")
        
        # Find log files in these directories
        log_files = []
        for exp_dir in experiment_dirs:
            logs = list(exp_dir.rglob("*.log"))
            log_files.extend(logs)
        
        if not log_files:
            print(f"No log files found in experiment directories with LLM model '{llm_model}'")
            return
        
        print(f"Found {len(log_files)} log files to analyze...")
        
        # Analyze each log file
        for log_file in log_files:
            print(f"Analyzing: {log_file}")
            try:
                analyzer = SemanticCorrectionAnalyzer(str(log_file))
                result = analyzer.analyze()
                self.results[str(log_file)] = result
            except Exception as e:
                print(f"Error analyzing {log_file}: {e}")
    
    def generate_comparative_report(self) -> Dict:
        """Generate a comparative analysis report across all analyzed files."""
        if not self.results:
            return {"error": "No analysis results available"}
        
        # Aggregate statistics
        all_attempts = []
        all_successful_attempts = []
        all_total_properties = []
        all_successful_properties = []
        all_failed_properties = []
        all_success_rates = []
        
        # Aggregate attempt distributions
        consolidated_total_dist = {}
        consolidated_successful_dist = {}
        
        # Aggregate LLM performance
        all_uniqueness_rates = []
        all_avg_unique_responses = []
        
        file_summaries = {}
        
        for file_path, result in self.results.items():
            stats = result['correction_statistics']
            
            # Basic statistics
            total_props = stats['total_properties_attempted']
            successful_props = stats['successful_corrections']
            failed_props = stats['failed_corrections']
            success_rate = stats['success_rate']
            
            all_total_properties.append(total_props)
            all_successful_properties.append(successful_props)
            all_failed_properties.append(failed_props)
            all_success_rates.append(success_rate)
            
            # Attempt distribution
            total_dist = result['attempt_distribution']['total']
            successful_dist = result['attempt_distribution']['successful']
            
            # Consolidate distributions
            for attempts, count in total_dist.items():
                if attempts not in consolidated_total_dist:
                    consolidated_total_dist[attempts] = 0
                consolidated_total_dist[attempts] += count
                all_attempts.extend([attempts] * count)
            
            for attempts, count in successful_dist.items():
                if attempts not in consolidated_successful_dist:
                    consolidated_successful_dist[attempts] = 0
                consolidated_successful_dist[attempts] += count
                all_successful_attempts.extend([attempts] * count)
            
            # LLM performance
            llm_perf = result['llm_performance']
            if llm_perf['assertions_analyzed'] > 0:
                all_uniqueness_rates.append(llm_perf['uniqueness_rate'])
                all_avg_unique_responses.append(llm_perf['average_unique_responses_per_assertion'])
            
            # File summary
            file_summaries[file_path] = {
                'total_properties': total_props,
                'successful_corrections': successful_props,
                'failed_corrections': failed_props,
                'success_rate': success_rate,
                'average_attempts': stats['average_attempts_total'],
                'llm_uniqueness_rate': llm_perf['uniqueness_rate'] if llm_perf['assertions_analyzed'] > 0 else 0
            }
        
        # Calculate comparative statistics
        comparative_stats = {
            'summary': {
                'files_analyzed': len(self.results),
                'files_with_corrections': sum(1 for r in self.results.values() if r['correction_statistics']['total_properties_attempted'] > 0),
                'total_properties_attempted': sum(all_total_properties),
                'total_successful_corrections': sum(all_successful_properties),
                'total_failed_corrections': sum(all_failed_properties),
                'overall_success_rate': sum(all_successful_properties) / sum(all_total_properties) if sum(all_total_properties) > 0 else 0,
                'average_attempts_across_all': statistics.mean(all_attempts) if all_attempts else 0,
                'average_attempts_successful_only': statistics.mean(all_successful_attempts) if all_successful_attempts else 0,
                'average_llm_uniqueness_rate': statistics.mean(all_uniqueness_rates) if all_uniqueness_rates else 0,
                'average_unique_responses_per_assertion': statistics.mean(all_avg_unique_responses) if all_avg_unique_responses else 0
            },
            'attempt_distribution': {
                'total': consolidated_total_dist,
                'successful': consolidated_successful_dist
            },
            'file_summaries': file_summaries
        }
        
        return {
            'comparative_statistics': comparative_stats,
            'individual_results': self.results
        }
    
    def generate_batch_pie_charts(self, report: Dict, output_dir: str = "charts", filter_info: str = ""):
        """Generate consolidated pie charts for all analyzed files."""
        if not MATPLOTLIB_AVAILABLE:
            print("Matplotlib not available. Cannot generate pie charts.")
            return
        
        # Create output directory
        Path(output_dir).mkdir(exist_ok=True)
        
        # Extract consolidated data
        comp_stats = report['comparative_statistics']
        total_dist = comp_stats['attempt_distribution']['total']
        successful_dist = comp_stats['attempt_distribution']['successful']
        
        if not total_dist:
            print("No consolidated attempt distribution data to visualize.")
            return
        
        # Create figure with better layout management
        fig = plt.figure(figsize=(16, 8))
        
        # Use GridSpec for better control over subplot positioning
        gs = fig.add_gridspec(1, 2, width_ratios=[1, 1], wspace=0.3, 
                             left=0.05, right=0.95, top=0.85, bottom=0.15)
        
        ax1 = fig.add_subplot(gs[0, 0])
        ax2 = fig.add_subplot(gs[0, 1])
        
        # Set main title with proper positioning
        title_base = f'Consolidated Semantic Correction Attempt Distribution\nAcross {comp_stats["summary"]["files_analyzed"]} Files'
        if filter_info:
            title_base += f' ({filter_info})'
        fig.suptitle(title_base, fontsize=16, fontweight='bold', y=0.95)
        
        # Fixed colors for each attempt count and outcome (consistent with individual analyzer)
        attempt_colors = {
            1: '#2ecc71',  # Green - 1 attempt (always successful)
            2: '#3498db',  # Blue - 2 attempts (always successful)
            3: '#f39c12',  # Orange - 3 attempts (always successful)
            4: '#e74c3c',  # Red - 4 attempts (always successful)
            '5_success': '#9b59b6',   # Purple - 5 attempts successful
            '5_failure': '#34495e'    # Dark gray - 5 attempts failed
        }
        
        # Chart 1: Enhanced attempts distribution showing success/failure for 5 attempts
        # Calculate consolidated 5-attempt successes and failures across all files
        enhanced_data = []
        enhanced_labels = []
        enhanced_colors = []
        
        # We need to recalculate from individual results to separate 5-attempt outcomes
        consolidated_5_success = 0
        consolidated_5_failure = 0
        
        # Go through individual results to count 5-attempt outcomes
        for file_path, result in report['individual_results'].items():
            details = result['attempt_distribution']['by_attempt_details']
            if 5 in details:
                consolidated_5_success += len(details[5].get('successful', []))
                consolidated_5_failure += len(details[5].get('failed', []))
        
        for attempts in sorted(total_dist.keys()):
            if attempts == 5:
                # Split 5 attempts into successful and failed
                if consolidated_5_success > 0:
                    enhanced_data.append(consolidated_5_success)
                    enhanced_labels.append(f'5 attempts\n(success: {consolidated_5_success})')
                    enhanced_colors.append(attempt_colors['5_success'])
                
                if consolidated_5_failure > 0:
                    enhanced_data.append(consolidated_5_failure)
                    enhanced_labels.append(f'5 attempts\n(failed: {consolidated_5_failure})')
                    enhanced_colors.append(attempt_colors['5_failure'])
            else:
                # For 1-4 attempts, all are successful
                total_count = total_dist[attempts]
                enhanced_data.append(total_count)
                enhanced_labels.append(f'{attempts} attempt{"s" if attempts > 1 else ""}\n({total_count} properties)')
                enhanced_colors.append(attempt_colors[attempts])
        
        attempts = enhanced_data
        counts = enhanced_data
        labels = enhanced_labels
        chart_colors = enhanced_colors
        
        wedges1, texts1, autotexts1 = ax1.pie(counts, labels=labels, autopct='%1.1f%%', 
                                              colors=chart_colors, startangle=90,
                                              labeldistance=1.1, pctdistance=0.85)
        ax1.set_title('All Correction Attempts', fontweight='bold', fontsize=14, pad=20)
        
        # Chart 2: Successful attempts distribution
        if successful_dist:
            succ_attempts = list(successful_dist.keys())
            succ_counts = list(successful_dist.values())
            succ_labels = [f'{att} attempt{"s" if att > 1 else ""}\n({count} successful)' 
                          for att, count in zip(succ_attempts, succ_counts)]
            
            # Use consistent colors for successful attempts too
            succ_chart_colors = [attempt_colors.get(att, '#95a5a6') for att in succ_attempts]
            
            wedges2, texts2, autotexts2 = ax2.pie(succ_counts, labels=succ_labels, autopct='%1.1f%%',
                                                  colors=succ_chart_colors, startangle=90,
                                                  labeldistance=1.1, pctdistance=0.85)
            ax2.set_title('Successful Corrections Only', fontweight='bold', fontsize=14, pad=20)
        else:
            ax2.text(0.5, 0.5, 'No Successful\nCorrections', ha='center', va='center', 
                    transform=ax2.transAxes, fontsize=16, fontweight='bold')
            ax2.set_title('Successful Corrections Only', fontweight='bold', fontsize=14, pad=20)
        
        # Style improvements
        for autotext in autotexts1:
            autotext.set_color('white')
            autotext.set_fontweight('bold')
            autotext.set_fontsize(10)
        
        if successful_dist:
            for autotext in autotexts2:
                autotext.set_color('white')
                autotext.set_fontweight('bold')
                autotext.set_fontsize(10)
        
        # Improve text positioning and size
        for text in texts1:
            text.set_fontsize(9)
            text.set_fontweight('normal')
        
        if successful_dist:
            for text in texts2:
                text.set_fontsize(9)
                text.set_fontweight('normal')
        
        # Ensure equal aspect ratio for both pie charts
        ax1.axis('equal')
        ax2.axis('equal')
        
        # Add a consistent color legend at the bottom (same as individual analyzer)
        legend_elements = [
            mpatches.Patch(color='#2ecc71', label='1 attempt (success)'),
            mpatches.Patch(color='#3498db', label='2 attempts (success)'),
            mpatches.Patch(color='#f39c12', label='3 attempts (success)'),
            mpatches.Patch(color='#e74c3c', label='4 attempts (success)'),
            mpatches.Patch(color='#9b59b6', label='5 attempts (success)'),
            mpatches.Patch(color='#34495e', label='5 attempts (failed)')
        ]
        
        # Add legend below the charts
        fig.legend(handles=legend_elements, loc='lower center', 
                  bbox_to_anchor=(0.5, 0.02), ncol=3, fontsize=9)
        
        # Save chart with high DPI and tight layout
        if filter_info:
            # Sanitize filter info for filename
            safe_filter = filter_info.replace('/', '_').replace('\\', '_').replace(':', '_').replace(' ', '_')
            output_file = f"{output_dir}/consolidated_attempt_distribution_{safe_filter}.png"
        else:
            output_file = f"{output_dir}/consolidated_attempt_distribution.png"
        plt.savefig(output_file, dpi=300, bbox_inches='tight', facecolor='white', 
                   edgecolor='none', pad_inches=0.2)
        print(f"\n📊 Consolidated pie charts saved to: {output_file}")
        
        # Only attempt to show chart if running in interactive mode
        if hasattr(plt.get_backend(), 'show') and plt.get_backend() != 'Agg':
            try:
                plt.show()
            except Exception:
                pass  # Non-interactive environment or no display
        
        plt.close()
    
    def print_comparative_summary(self, report: Dict):
        """Print a summary of comparative statistics."""
        print("=" * 80)
        print("BATCH SEMANTIC CORRECTION LOOP ANALYSIS")
        print("=" * 80)
        
        comp_stats = report['comparative_statistics']
        summary = comp_stats['summary']
        
        print(f"Files analyzed: {summary['files_analyzed']}")
        print(f"Files with corrections: {summary['files_with_corrections']}")
        print()
        
        # Overall statistics
        if 'total_properties_attempted' in summary:
            print("OVERALL STATISTICS ACROSS ALL FILES:")
            stats_to_show = [
                ('total_properties_attempted', 'Total properties attempted'),
                ('total_successful_corrections', 'Total successful corrections'),
                ('total_failed_corrections', 'Total failed corrections'),
                ('overall_success_rate', 'Overall success rate'),
                ('average_attempts_across_all', 'Average attempts (all properties)'),
                ('average_attempts_successful_only', 'Average attempts (successful only)'),
                ('average_llm_uniqueness_rate', 'Average LLM uniqueness rate'),
                ('average_unique_responses_per_assertion', 'Average unique responses per assertion')
            ]
            
            for stat_key, stat_label in stats_to_show:
                value = summary.get(stat_key, 0)
                if 'rate' in stat_key.lower():
                    print(f"  {stat_label}: {value:.1%}")
                else:
                    print(f"  {stat_label}: {value:.2f}")
            print()
        
        # Consolidated attempt distribution
        total_dist = comp_stats['attempt_distribution']['total']
        successful_dist = comp_stats['attempt_distribution']['successful']
        
        if total_dist:
            print("CONSOLIDATED ATTEMPT DISTRIBUTION:")
            print("  Attempts | Total | Successful | Success Rate")
            print("  ---------|-------|------------|-------------")
            
            total_properties = sum(total_dist.values())
            
            for i in range(1, 6):
                total_count = total_dist.get(i, 0)
                success_count = successful_dist.get(i, 0)
                success_rate = success_count / total_count if total_count > 0 else 0
                percentage_of_total = total_count / total_properties if total_properties > 0 else 0
                print(f"      {i}    |  {total_count:4d} |     {success_count:4d}   |    {success_rate:6.1%}   ({percentage_of_total:5.1%} of all)")
            print()
        
        # Analysis insights
        print("KEY INSIGHTS:")
        if total_dist:
            one_attempt = total_dist.get(1, 0)
            five_attempts = total_dist.get(5, 0)
            print(f"  • Quick wins: {one_attempt} properties ({one_attempt/total_properties*100:.1f}%) needed only 1 attempt")
            print(f"  • Difficult cases: {five_attempts} properties ({five_attempts/total_properties*100:.1f}%) needed maximum 5 attempts")
            
            # Success rate by attempt count
            for i in range(1, 6):
                total_count = total_dist.get(i, 0)
                success_count = successful_dist.get(i, 0)
                if total_count > 0:
                    success_rate = success_count / total_count
                    print(f"  • {i}-attempt success rate: {success_rate:.1%} ({success_count}/{total_count})")
        
        print()
        
        # File-by-file summary
        print("FILE-BY-FILE SUMMARY:")
        print("File | Props | Success | Rate | Avg Attempts | LLM Uniq")
        print("-----|-------|---------|------|-------------|----------")
        
        file_summaries = comp_stats['file_summaries']
        for file_path, summary_data in file_summaries.items():
            file_name = Path(file_path).name[:15] + "..." if len(Path(file_path).name) > 18 else Path(file_path).name
            print(f"{file_name:18} | {summary_data['total_properties']:5d} | {summary_data['successful_corrections']:7d} | "
                  f"{summary_data['success_rate']:4.1%} | {summary_data['average_attempts']:11.2f} | {summary_data['llm_uniqueness_rate']:8.1%}")
    
    def print_verbose_report(self, report: Dict):
        """Print individual detailed reports for each file."""
        print("=" * 80)
        print("INDIVIDUAL FILE REPORTS")
        print("=" * 80)
        
        for i, (file_path, result) in enumerate(report['individual_results'].items(), 1):
            print(f"\n{'='*20} FILE {i}/{len(report['individual_results'])} {'='*20}")
            analyzer = SemanticCorrectionAnalyzer(file_path)
            analyzer.print_summary(result)
            print()


def main():
    parser = argparse.ArgumentParser(
        description='Batch analysis of Semantic Correction Loops from multiple agent log files',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze all logs in a directory
  python3 src/analysis/batch_analyze_semantic_correction.py --directory svapshot_v1_results/
  
  # Analyze logs from specific timestamp experiments  
  python3 src/analysis/batch_analyze_semantic_correction.py --timestamp 20250823_014948 --directory svapshot_v1_results/
  
  # Analyze logs from specific LLM model experiments  
  python3 src/analysis/batch_analyze_semantic_correction.py --llm-model gpt41 --directory svapshot_v1_results/
  python3 src/analysis/batch_analyze_semantic_correction.py --llm-model llama_33_70b --directory svapshot_v1_results/
  
  # Analyze specific files with verbose output
  python3 src/analysis/batch_analyze_semantic_correction.py --files file1.log file2.log --verbose
  
  # Generate pie charts for specific LLM model
  python3 src/analysis/batch_analyze_semantic_correction.py --llm-model llama_405b --directory svapshot_v1_results/ --charts

  # Save verbose report to file
  python3 src/analysis/batch_analyze_semantic_correction.py --timestamp 20250823_014948 --directory svapshot_v1_results/ --verbose --report detailed_analysis.txt
        """
    )
    
    # Input options (mutually exclusive)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument('--directory', '-d', help='Directory to search for log files')
    input_group.add_argument('--files', '-f', nargs='+', help='Specific log files to analyze')
    
    # Directory options
    parser.add_argument('--timestamp', '-t', help='Filter by experiment timestamp (e.g., 20250823_014948)')
    parser.add_argument('--llm-model', '-llm', help='Filter by LLM model (e.g., gpt41, llama_33_70b, llama_405b)')
    parser.add_argument('--recursive', '-r', action='store_true', help='Search directories recursively')
    parser.add_argument('--pattern', '-p', default='*.log', help='File pattern to match (default: *.log)')
    
    # Output options
    parser.add_argument('--output', '-o', help='Output JSON file for detailed results')
    parser.add_argument('--verbose', '-v', action='store_true', 
                        help='Show detailed individual reports for each file')
    parser.add_argument('--charts', '-c', action='store_true', 
                        help='Generate consolidated pie charts (requires matplotlib)')
    parser.add_argument('--chart-dir', default='charts', 
                        help='Directory to save charts (default: charts)')
    parser.add_argument('--report', help='Save report to file (auto-generated if not specified)')
    parser.add_argument('--no-file', action='store_true', help='Do not save report to file (output only to console)')
    
    args = parser.parse_args()
    
    # Validate matplotlib availability for charts
    if args.charts and not MATPLOTLIB_AVAILABLE:
        print("Error: matplotlib is required for chart generation.")
        print("Install with: pip install matplotlib")
        sys.exit(1)
    
    # Initialize analyzer
    analyzer = BatchSemanticCorrectionAnalyzer()
    
    # Analyze files based on input method
    if args.directory:
        if args.timestamp:
            analyzer.filter_by_timestamp(args.directory, args.timestamp, args.recursive)
        elif args.llm_model:
            analyzer.filter_by_llm_model(args.directory, args.llm_model, args.recursive)
        else:
            analyzer.analyze_directory(args.directory, args.pattern)
    else:
        analyzer.analyze_files(args.files)
    
    # Check if any files were analyzed
    if not analyzer.results:
        print("No files were successfully analyzed.")
        sys.exit(1)
    
    # Generate comparative report
    report = analyzer.generate_comparative_report()
    
    # Determine output destination for reports
    report_file = None
    original_stdout = sys.stdout
    
    # Always save to file unless explicitly disabled
    if not args.no_file:
        if args.report:
            report_file = args.report
        else:
            # Auto-generate report filename based on analysis type
            if args.timestamp:
                if args.verbose:
                    report_file = f"detailed_batch_report_{args.timestamp}.txt"
                else:
                    report_file = f"summary_batch_report_{args.timestamp}.txt"
            elif args.llm_model:
                # Sanitize LLM model name for filename
                safe_llm_name = args.llm_model.replace('/', '_').replace('\\', '_').replace(':', '_').replace(' ', '_')
                if args.verbose:
                    report_file = f"detailed_batch_report_{safe_llm_name}.txt"
                else:
                    report_file = f"summary_batch_report_{safe_llm_name}.txt"
            elif args.files:
                if args.verbose:
                    report_file = f"detailed_batch_report_{len(args.files)}_files.txt"
                else:
                    report_file = f"summary_batch_report_{len(args.files)}_files.txt"
            else:
                if args.verbose:
                    report_file = "detailed_batch_report.txt"
                else:
                    report_file = "summary_batch_report.txt"
    
    if args.verbose:
        # Detailed analysis mode
        if report_file:
            print(f"Generating detailed batch report: {report_file}")
            
            # Save detailed report to file
            with open(report_file, 'w') as f:
                sys.stdout = f
                
                # Print summary first
                analyzer.print_comparative_summary(report)
                
                # Print verbose individual reports
                analyzer.print_verbose_report(report)
            
            # Restore stdout
            sys.stdout = original_stdout
            print(f"Detailed batch report saved to: {report_file}")
        
        # Also show brief summary on console
        comp_stats = report['comparative_statistics']['summary']
        print(f"Batch analysis complete: {comp_stats['files_analyzed']} files analyzed")
        print(f"Overall success rate: {comp_stats['overall_success_rate']:.1%}")
        print(f"Average attempts: {comp_stats['average_attempts_across_all']:.2f}")
        
        if not report_file:
            # If no file output, show detailed summary on console
            analyzer.print_comparative_summary(report)
            analyzer.print_verbose_report(report)
    else:
        # Summary mode - always show on console
        analyzer.print_comparative_summary(report)
        
        # Also save to file if not disabled
        if report_file:
            with open(report_file, 'w') as f:
                sys.stdout = f
                analyzer.print_comparative_summary(report)
            # Restore stdout
            sys.stdout = original_stdout
            print(f"\nSummary report saved to: {report_file}")
    
    # Save detailed results if requested
    if args.output:
        with open(args.output, 'w') as f:
            json.dump(report, f, indent=2)
        print(f"Detailed results saved to {args.output}")
    
    # Generate charts if requested
    if args.charts:
        # Create filter info for chart titles and filenames
        filter_info = ""
        if args.timestamp:
            filter_info = f"Timestamp: {args.timestamp}"
        elif args.llm_model:
            filter_info = f"LLM: {args.llm_model}"
        
        analyzer.generate_batch_pie_charts(report, args.chart_dir, filter_info)


if __name__ == "__main__":
    main() 