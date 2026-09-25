#!/usr/bin/env python3
"""
Post-processing script for analyzing Semantic Correction Loop from agent log files.

This script extracts and analyzes statistics about the semantic correction process:
- Average number of correction attempts (total and successful only)
- Percentiles of correction attempts
- LLM performance analysis: how often the LLM changes assertions vs repeats them
- Pie chart visualization of attempt distribution
"""

import re
import argparse
import json
import sys
from pathlib import Path
from collections import defaultdict, Counter
from typing import Dict, List, Tuple, Set
import statistics

# Try to import matplotlib, but make it optional
try:
    import warnings
    # Suppress common matplotlib warnings
    warnings.filterwarnings('ignore', category=UserWarning, module='matplotlib')
    warnings.filterwarnings('ignore', message='.*GUI is implemented.*')
    warnings.filterwarnings('ignore', message='.*backend.*')
    
    import matplotlib
    import os
    # Use non-interactive backend by default, but allow override via environment variable
    if os.environ.get('MATPLOTLIB_INTERACTIVE', '').lower() not in ('1', 'true', 'yes'):
        matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    print("Warning: matplotlib not available. Pie charts will not be generated.")
    print("Install with: pip install matplotlib")

class SemanticCorrectionAnalyzer:
    def __init__(self, log_file_path: str):
        self.log_file_path = log_file_path
        self.correction_attempts = {}  # property_name -> list of attempts
        self.successful_corrections = set()  # property names that were successfully corrected
        self.failed_corrections = set()  # property names that failed
        self.assertion_responses = defaultdict(list)  # property_name -> list of assertion texts
        
    def parse_log_file(self):
        """Parse the log file to extract semantic correction information."""
        current_property = None
        attempt_count = 0
        
        with open(self.log_file_path, 'r') as f:
            lines = f.readlines()
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            # Look for semantic correction start
            if "Semantic correction loop" in line and "attempt" in line:
                # Extract property name and attempt number
                match = re.search(r'for property\s+(\S+).*attempt\s+(\d+)', line)
                if match:
                    property_name = match.group(1)
                    attempt_num = int(match.group(2))
                    
                    if property_name not in self.correction_attempts:
                        self.correction_attempts[property_name] = []
                    
                    # Look for the assertion in subsequent lines
                    assertion_text = self._extract_assertion_from_correction(lines, i)
                    if assertion_text:
                        self.assertion_responses[property_name].append(assertion_text)
                    
                    current_property = property_name
                    attempt_count = attempt_num
            
            # Look for correction results
            elif "Successfully fixed assertion" in line and "after" in line and "attempts" in line:
                match = re.search(r'Successfully fixed assertion\s+(\S+).*after\s+(\d+)\s+attempts', line)
                if match:
                    property_name = match.group(1)
                    total_attempts = int(match.group(2))
                    self.correction_attempts[property_name] = list(range(1, total_attempts + 1))
                    self.successful_corrections.add(property_name)
            
            elif "Failed to fix assertion" in line and "after" in line and "attempts" in line:
                match = re.search(r'Failed to fix assertion\s+(\S+).*after\s+(\d+)\s+attempts', line)
                if match:
                    property_name = match.group(1)
                    total_attempts = int(match.group(2))
                    self.correction_attempts[property_name] = list(range(1, total_attempts + 1))
                    self.failed_corrections.add(property_name)
            
            i += 1
    
    def _extract_assertion_from_correction(self, lines: List[str], start_idx: int) -> str:
        """Extract assertion text from correction attempt."""
        # Look for assertion in the next few lines
        for j in range(start_idx + 1, min(start_idx + 10, len(lines))):
            line = lines[j].strip()
            if line.startswith("as__") or "assert property" in line:
                return line
        return ""
    
    def extract_llm_model_from_path(self) -> str:
        """Extract LLM model name from the log file path or containing directory."""
        path = Path(self.log_file_path)
        
        # Check all parent directories for experiment pattern: ft_{module}_{timestamp}_{llm_model}
        # Log files might be in subdirectories like sva/
        for parent_dir in [path.parent, path.parent.parent, path.parent.parent.parent]:
            dir_name = parent_dir.name
            if dir_name.startswith('ft_'):
                parts = dir_name.split('_')
                if len(parts) >= 4:
                    # Find timestamp pattern (8 digits followed by 6 digits)
                    timestamp_start_idx = None
                    for i, part in enumerate(parts):
                        if len(part) == 8 and part.isdigit():
                            # Check if next part is 6 digits (time part)
                            if i + 1 < len(parts) and len(parts[i + 1]) == 6 and parts[i + 1].isdigit():
                                timestamp_start_idx = i
                                break
                    
                    if timestamp_start_idx is not None and timestamp_start_idx + 2 < len(parts):
                        # LLM model is everything after timestamp
                        llm_model = '_'.join(parts[timestamp_start_idx + 2:])
                        return llm_model
        
        # Try to extract from the log file name itself
        file_name = path.stem
        
        # Look for common LLM model patterns in filename
        llm_patterns = [
            'gpt_41_mini', 'gpt_4_1_mini', 'gpt4', 'gpt41',
            'llama_33_70b', 'llama_405b', 'llama_70b', 'llama',
            'claude', 'anthropic', 'deepseek', 'nemotron',
            'o4_mini', 'o4mini', 'o3_mini', 'o3mini', 'o3'
        ]
        
        for pattern in llm_patterns:
            if pattern in file_name.lower():
                return pattern
        
        # Also check the full path for patterns
        full_path_str = str(path).lower()
        for pattern in llm_patterns:
            if pattern in full_path_str:
                return pattern
        
        return "unknown_llm"
    
    def analyze(self) -> Dict:
        """Perform the complete analysis and return results."""
        self.parse_log_file()
        
        # Calculate basic statistics
        all_attempts = []
        successful_attempts = []
        
        for prop_name, attempts in self.correction_attempts.items():
            attempt_count = len(attempts)
            all_attempts.append(attempt_count)
            
            if prop_name in self.successful_corrections:
                successful_attempts.append(attempt_count)
        
        # Attempt distribution
        total_distribution = Counter(all_attempts)
        successful_distribution = Counter(successful_attempts)
        
        # LLM Performance Analysis
        llm_analysis = self._analyze_llm_performance()
        
        # Calculate percentiles for attempts
        percentiles = {}
        if all_attempts:
            for p in [25, 50, 75, 90, 95]:
                percentiles[f"p{p}"] = statistics.quantiles(all_attempts, n=100)[p-1] if len(all_attempts) > 1 else all_attempts[0]
        
        return {
            "log_file": self.log_file_path,
            "llm_model": self.extract_llm_model_from_path(),
            "correction_statistics": {
                "total_properties_attempted": len(self.correction_attempts),
                "successful_corrections": len(self.successful_corrections),
                "failed_corrections": len(self.failed_corrections),
                "success_rate": len(self.successful_corrections) / len(self.correction_attempts) if self.correction_attempts else 0,
                "average_attempts_total": statistics.mean(all_attempts) if all_attempts else 0,
                "average_attempts_successful": statistics.mean(successful_attempts) if successful_attempts else 0,
                "attempt_percentiles": percentiles
            },
            "attempt_distribution": {
                "total": dict(total_distribution),
                "successful": dict(successful_distribution),
                "by_attempt_details": self._get_attempt_details()
            },
            "llm_performance": llm_analysis
        }
    
    def _get_attempt_details(self) -> Dict:
        """Get detailed breakdown of which assertions belong to each attempt count."""
        details = {}
        
        for prop_name, attempts in self.correction_attempts.items():
            attempt_count = len(attempts)
            if attempt_count not in details:
                details[attempt_count] = {
                    "total": [],
                    "successful": [],
                    "failed": []
                }
            
            details[attempt_count]["total"].append(prop_name)
            
            if prop_name in self.successful_corrections:
                details[attempt_count]["successful"].append(prop_name)
            else:
                details[attempt_count]["failed"].append(prop_name)
        
        return details
    
    def _analyze_llm_performance(self) -> Dict:
        """Analyze how often the LLM generates unique vs repeated responses."""
        analysis = {
            "assertions_analyzed": 0,
            "assertions_with_unique_responses": 0,
            "total_responses": 0,
            "total_unique_responses": 0,
            "uniqueness_rate": 0.0,
            "average_unique_responses_per_assertion": 0.0,
            "detailed_analysis": {}
        }
        
        assertions_with_multiple_attempts = {k: v for k, v in self.assertion_responses.items() 
                                           if len(v) >= 2}
        
        if not assertions_with_multiple_attempts:
            return analysis
        
        unique_counts = []
        
        for prop_name, responses in assertions_with_multiple_attempts.items():
            unique_responses = set(responses)
            unique_count = len(unique_responses)
            total_responses = len(responses)
            
            unique_counts.append(unique_count)
            
            analysis["detailed_analysis"][prop_name] = {
                "total_responses": total_responses,
                "unique_responses": unique_count,
                "uniqueness_ratio": unique_count / total_responses,
                "responses": responses,
                "unique_responses_list": list(unique_responses)
            }
        
        analysis["assertions_analyzed"] = len(assertions_with_multiple_attempts)
        analysis["assertions_with_unique_responses"] = sum(1 for count in unique_counts if count > 1)
        analysis["total_responses"] = sum(len(responses) for responses in assertions_with_multiple_attempts.values())
        analysis["total_unique_responses"] = sum(unique_counts)
        analysis["uniqueness_rate"] = analysis["assertions_with_unique_responses"] / analysis["assertions_analyzed"]
        analysis["average_unique_responses_per_assertion"] = statistics.mean(unique_counts) if unique_counts else 0
        
        return analysis
    
    def generate_pie_charts(self, report: Dict, output_dir: str = "charts"):
        """Generate pie charts for attempt distribution."""
        if not MATPLOTLIB_AVAILABLE:
            print("Matplotlib not available. Cannot generate pie charts.")
            return
        
        # Create output directory
        Path(output_dir).mkdir(exist_ok=True)
        
        # Extract data for charts
        total_dist = report['attempt_distribution']['total']
        successful_dist = report['attempt_distribution']['successful']
        
        if not total_dist:
            print("No attempt distribution data to visualize.")
            return
        
        # Create figure with better layout management
        fig = plt.figure(figsize=(16, 8))
        
        # Use GridSpec for better control over subplot positioning
        gs = fig.add_gridspec(1, 2, width_ratios=[1, 1], wspace=0.3, 
                             left=0.05, right=0.95, top=0.85, bottom=0.15)
        
        ax1 = fig.add_subplot(gs[0, 0])
        ax2 = fig.add_subplot(gs[0, 1])
        
        # Set main title with proper positioning
        fig.suptitle(f'Semantic Correction Attempt Distribution\n{Path(self.log_file_path).name}', 
                     fontsize=16, fontweight='bold', y=0.95)
        
        # Fixed colors for each attempt count and outcome
        attempt_colors = {
            1: '#2ecc71',  # Green - 1 attempt (always successful)
            2: '#3498db',  # Blue - 2 attempts (always successful)
            3: '#f39c12',  # Orange - 3 attempts (always successful)
            4: '#e74c3c',  # Red - 4 attempts (always successful)
            '5_success': '#9b59b6',   # Purple - 5 attempts successful
            '5_failure': '#34495e'    # Dark gray - 5 attempts failed
        }
        
        # Chart 1: Enhanced attempts distribution showing success/failure for 5 attempts
        # Separate 5-attempt cases into success and failure
        enhanced_data = []
        enhanced_labels = []
        enhanced_colors = []
        
        # Get detailed breakdown to separate 5-attempt successes and failures
        details = report['attempt_distribution']['by_attempt_details']
        
        for attempts in sorted(total_dist.keys()):
            total_count = total_dist[attempts]
            
            if attempts == 5:
                # Split 5 attempts into successful and failed
                successful_5 = len(details.get(5, {}).get('successful', []))
                failed_5 = len(details.get(5, {}).get('failed', []))
                
                if successful_5 > 0:
                    enhanced_data.append(successful_5)
                    enhanced_labels.append(f'5 attempts\n(success: {successful_5})')
                    enhanced_colors.append(attempt_colors['5_success'])
                
                if failed_5 > 0:
                    enhanced_data.append(failed_5)
                    enhanced_labels.append(f'5 attempts\n(failed: {failed_5})')
                    enhanced_colors.append(attempt_colors['5_failure'])
            else:
                # For 1-4 attempts, all are successful (since they wouldn't continue if failed)
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
        
        # Add a consistent color legend at the bottom
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
        output_file = f"{output_dir}/attempt_distribution_{Path(self.log_file_path).stem}.png"
        plt.savefig(output_file, dpi=300, bbox_inches='tight', facecolor='white', 
                   edgecolor='none', pad_inches=0.2)
        print(f"\n📊 Pie charts saved to: {output_file}")
        
        # Show chart if interactive mode is enabled
        if os.environ.get('MATPLOTLIB_INTERACTIVE', '').lower() in ('1', 'true', 'yes'):
            try:
                plt.show()
            except Exception:
                pass  # Non-interactive environment or no display
        
        plt.close()
    
    def print_summary(self, report: Dict):
        """Print a human-readable summary of the analysis."""
        print("=" * 80)
        print("SEMANTIC CORRECTION LOOP ANALYSIS")
        print("=" * 80)
        print(f"Log file: {report['log_file']}")
        print(f"LLM model: {report.get('llm_model', 'unknown')}")
        print()
        
        # Correction Statistics
        corr_stats = report['correction_statistics']
        print("CORRECTION STATISTICS:")
        print(f"  Total properties attempted: {corr_stats.get('total_properties_attempted', 0)}")
        print(f"  Successful corrections: {corr_stats.get('successful_corrections', 0)}")
        print(f"  Failed corrections: {corr_stats.get('failed_corrections', 0)}")
        print(f"  Success rate: {corr_stats.get('success_rate', 0):.1%}")
        print(f"  Average attempts (total): {corr_stats.get('average_attempts_total', 0):.2f}")
        print(f"  Average attempts (successful only): {corr_stats.get('average_attempts_successful', 0):.2f}")
        print()
        
        # Attempt Distribution
        print("ATTEMPT DISTRIBUTION:")
        print("  Attempts | Total | Successful")
        print("  ---------|-------|----------")
        
        attempt_dist = report['attempt_distribution']
        total_dist = attempt_dist['total']
        successful_dist = attempt_dist['successful']
        details = attempt_dist['by_attempt_details']
        
        if total_dist:
            for i in range(1, 6):
                total_count = total_dist.get(i, 0)
                success_count = successful_dist.get(i, 0)
                print(f"      {i}    |   {total_count:2d}  |     {success_count:2d}")
            print()
            
            # Show assertion names for each category
            print("DETAILED ATTEMPT BREAKDOWN:")
            for attempts in sorted(details.keys()):
                detail = details[attempts]
                total_props = detail['total']
                successful_props = detail['successful']
                failed_props = detail['failed']
                
                print(f"\n  {attempts} attempt{'s' if attempts > 1 else ''} ({len(total_props)} total):")
                if successful_props:
                    print(f"    ✅ Successful ({len(successful_props)}): {', '.join(successful_props)}")
                if failed_props:
                    print(f"    ❌ Failed ({len(failed_props)}): {', '.join(failed_props)}")
        
        # LLM Performance
        llm_perf = report['llm_performance']
        print("\nLLM PERFORMANCE ANALYSIS:")
        print(f"  Assertions analyzed (2+ attempts): {llm_perf.get('assertions_analyzed', 0)}")
        print(f"  Assertions with unique responses: {llm_perf.get('assertions_with_unique_responses', 0)}")
        print(f"  LLM uniqueness rate: {llm_perf.get('uniqueness_rate', 0):.1%}")
        print(f"  Average unique responses per assertion: {llm_perf.get('average_unique_responses_per_assertion', 0):.2f}")
        
        # Detailed LLM analysis
        detailed = llm_perf.get('detailed_analysis', {})
        if detailed:
            print("\n  DETAILED LLM RESPONSE ANALYSIS:")
            
            # Group by uniqueness characteristics
            always_unique = []
            sometimes_unique = []
            never_unique = []
            
            for prop_name, analysis in detailed.items():
                total_responses = analysis['total_responses']
                unique_responses = analysis['unique_responses']
                
                if unique_responses == total_responses:
                    always_unique.append(prop_name)
                elif unique_responses > 1:
                    sometimes_unique.append(prop_name)
                else:
                    never_unique.append(prop_name)
            
            if always_unique:
                print(f"    🎯 Always unique responses ({len(always_unique)}): {', '.join(always_unique)}")
            if sometimes_unique:
                print(f"    🔄 Sometimes unique responses ({len(sometimes_unique)}): {', '.join(sometimes_unique)}")
            if never_unique:
                print(f"    🔁 Never unique responses ({len(never_unique)}): {', '.join(never_unique)}")


def main():
    parser = argparse.ArgumentParser(description='Analyze semantic correction loops from agent logs')
    parser.add_argument('log_file', help='Path to the agent log file')
    parser.add_argument('--output', '-o', help='Output JSON file for detailed results')
    parser.add_argument('--verbose', '-v', action='store_true', help='Show detailed analysis')
    parser.add_argument('--charts', '-c', action='store_true', help='Generate pie charts (requires matplotlib)')
    parser.add_argument('--chart-dir', default='charts', help='Directory to save charts (default: charts)')
    parser.add_argument('--report', '-r', help='Save report to file (auto-generated if not specified)')
    parser.add_argument('--no-file', action='store_true', help='Do not save report to file (output only to console)')
    
    args = parser.parse_args()
    
    if not Path(args.log_file).exists():
        print(f"Error: Log file '{args.log_file}' not found.")
        sys.exit(1)
    
    analyzer = SemanticCorrectionAnalyzer(args.log_file)
    report = analyzer.analyze()
    
    # Determine output destination for report
    report_file = None
    original_stdout = sys.stdout
    
    # Always save to file unless explicitly disabled
    if not args.no_file:
        if args.report:
            report_file = args.report
        else:
            # Auto-generate report filename based on input log file
            log_stem = Path(args.log_file).stem
            if args.verbose:
                report_file = f"detailed_report_{log_stem}.txt"
            else:
                report_file = f"summary_report_{log_stem}.txt"
    
    if args.verbose:
        # Detailed analysis
        if report_file:
            print(f"Generating detailed report: {report_file}")
            # Save detailed report to file
            with open(report_file, 'w') as f:
                sys.stdout = f
                analyzer.print_summary(report)
            # Restore stdout and show brief summary on console
            sys.stdout = original_stdout
            print(f"Detailed report saved to: {report_file}")
        
        # Also show brief summary on console
        stats = report['correction_statistics']
        print(f"Analysis of {args.log_file}:")
        print(f"  Properties attempted: {stats['total_properties_attempted']}")
        print(f"  Success rate: {stats['success_rate']:.1%}")
        print(f"  Average attempts: {stats['average_attempts_total']:.2f}")
        
        if not report_file:
            # If no file output, show detailed summary on console
            analyzer.print_summary(report)
    else:
        # Brief summary mode
        stats = report['correction_statistics']
        summary_output = f"""Analysis of {args.log_file}:
  Properties attempted: {stats['total_properties_attempted']}
  Success rate: {stats['success_rate']:.1%}
  Average attempts: {stats['average_attempts_total']:.2f}

CORRECTION STATISTICS:
  Total properties attempted: {stats['total_properties_attempted']}
  Successful corrections: {stats['successful_corrections']}
  Failed corrections: {stats['failed_corrections']}
  Success rate: {stats['success_rate']:.1%}
  Average attempts (total): {stats['average_attempts_total']:.2f}
  Average attempts (successful only): {stats['average_attempts_successful']:.2f}

ATTEMPT DISTRIBUTION:
  Properties needing 1 attempt: {report['attempt_distribution']['total'].get(1, 0)}
  Properties needing 2 attempts: {report['attempt_distribution']['total'].get(2, 0)}
  Properties needing 3 attempts: {report['attempt_distribution']['total'].get(3, 0)}
  Properties needing 4 attempts: {report['attempt_distribution']['total'].get(4, 0)}
  Properties needing 5 attempts: {report['attempt_distribution']['total'].get(5, 0)}

LLM PERFORMANCE:
  Assertions analyzed for uniqueness: {report['llm_performance']['assertions_analyzed']}
  Assertions with unique responses: {report['llm_performance']['assertions_with_unique_responses']}
  LLM uniqueness rate: {report['llm_performance']['uniqueness_rate']:.1%}
  Average unique responses per assertion: {report['llm_performance']['average_unique_responses_per_assertion']:.2f}
"""
        
        # Always show on console
        print(summary_output.strip())
        
        # Also save to file if not disabled
        if report_file:
            with open(report_file, 'w') as f:
                f.write(summary_output)
            print(f"\nSummary report saved to: {report_file}")
    
    if args.output:
        with open(args.output, 'w') as f:
            json.dump(report, f, indent=2)
        print(f"Detailed results saved to {args.output}")
    
    if args.charts:
        analyzer.generate_pie_charts(report, args.chart_dir)


if __name__ == "__main__":
    main() 