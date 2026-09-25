#!/usr/bin/env python3
"""
Unified Semantic Correction Figure Generator

This script generates a single figure with 4 pie chart panels showing semantic correction
attempt distributions for all modules (div_unit, mul_unit, i2c, ptw) in clockwise order.
The figure includes a single shared color legend at the bottom.

Author: AI Assistant
"""

import argparse
import sys
from pathlib import Path
from batch_analyze_semantic_correction import BatchSemanticCorrectionAnalyzer

# Try to import matplotlib
try:
    import warnings
    warnings.filterwarnings('ignore', category=UserWarning, module='matplotlib')
    warnings.filterwarnings('ignore', message='.*GUI is implemented.*')
    warnings.filterwarnings('ignore', message='.*backend.*')
    
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import matplotlib.gridspec as gridspec
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    print("Error: matplotlib is required for this script.")
    sys.exit(1)

def generate_unified_semantic_correction_figure(output_dir: str = "charts"):
    """Generate unified 2x2 semantic correction figure for all modules."""
    
    # Module mapping and order (clockwise: top-left, top-right, bottom-right, bottom-left)
    # Map actual module names to display names and their timestamps
    modules = {
        'hl_div': {'position': (0, 0), 'title': 'Div Unit', 'timestamp': '20250822_081853'},
        'hl_mul_unit': {'position': (0, 1), 'title': 'Mul Unit', 'timestamp': '20250824_140034'}, 
        'i2c_master': {'position': (1, 1), 'title': 'I2C', 'timestamp': '20250825_232658'},
        'ptw': {'position': (1, 0), 'title': 'PTW', 'timestamp': '20250823_014948'}
    }
    
    # Create output directory
    Path(output_dir).mkdir(exist_ok=True)
    
    # Consistent color scheme (same as individual analyzers)
    attempt_colors = {
        1: '#2ecc71',  # Green - 1 attempt (always successful)
        2: '#3498db',  # Blue - 2 attempts (always successful)
        3: '#f39c12',  # Orange - 3 attempts (always successful)
        4: '#e74c3c',  # Red - 4 attempts (always successful)
        '5_success': '#9b59b6',   # Purple - 5 attempts successful
        '5_failure': '#34495e'    # Dark gray - 5 attempts failed
    }
    
    # Create figure with 2x2 subplots, each containing 2 pie charts
    fig = plt.figure(figsize=(24, 18))
    gs = gridspec.GridSpec(2, 2, figure=fig, hspace=0.25, wspace=0.2,
                          left=0.05, right=0.95, top=0.85, bottom=0.12)
    
    # Set main title with better vertical spacing
    fig.suptitle(f'Semantic Correction Attempt Distribution Across All Modules',
                fontsize=32, fontweight='bold', y=0.92)
    
    # Process each module
    all_charts_data = {}
    
    for module_key, module_info in modules.items():
        print(f"Processing {module_info['title']} module...")
        
        # Initialize analyzer for this module
        analyzer = BatchSemanticCorrectionAnalyzer()
        
        # Filter logs by the specific timestamp for this module
        base_dir = "svapshot_v1_results"
        module_timestamp = module_info['timestamp']
        analyzer.filter_by_timestamp(base_dir, module_timestamp)
        
        # Filter results to only include this module
        filtered_results = {}
        for file_path, result in analyzer.results.items():
            if module_key in file_path.lower():
                filtered_results[file_path] = result
        
        analyzer.results = filtered_results
        
        if not analyzer.results:
            print(f"  No data found for {module_info['title']}")
            continue
        
        # Generate report for this module
        report = analyzer.generate_comparative_report()
        
        # Extract data for pie chart
        comp_stats = report['comparative_statistics']
        total_dist = comp_stats['attempt_distribution']['total']
        
        if not total_dist:
            print(f"  No attempt distribution data for {module_info['title']}")
            continue
        
        # Prepare enhanced data (split 5 attempts into success/failure)
        enhanced_data = []
        enhanced_labels = []
        enhanced_colors = []
        
        # Calculate 5-attempt success/failure from individual results
        consolidated_5_success = 0
        consolidated_5_failure = 0
        
        for file_path, result in report['individual_results'].items():
            details = result['attempt_distribution']['by_attempt_details']
            if 5 in details:
                consolidated_5_success += len(details[5].get('successful', []))
                consolidated_5_failure += len(details[5].get('failed', []))
        
        for attempts in sorted(total_dist.keys()):
            if attempts == 5:
                if consolidated_5_success > 0:
                    enhanced_data.append(consolidated_5_success)
                    enhanced_labels.append(f'5 (success)\n({consolidated_5_success})')
                    enhanced_colors.append(attempt_colors['5_success'])
                
                if consolidated_5_failure > 0:
                    enhanced_data.append(consolidated_5_failure)
                    enhanced_labels.append(f'5 (failed)\n({consolidated_5_failure})')
                    enhanced_colors.append(attempt_colors['5_failure'])
            else:
                total_count = total_dist[attempts]
                enhanced_data.append(total_count)
                enhanced_labels.append(f'{attempts}\n({total_count})')
                enhanced_colors.append(attempt_colors[attempts])
        
        all_charts_data[module_key] = {
            'data': enhanced_data,
            'labels': enhanced_labels,
            'colors': enhanced_colors,
            'title': module_info['title'],
            'report': report
        }
    
    # Create pie charts for each module (2 charts per module)
    for module_key, module_info in modules.items():
        if module_key not in all_charts_data:
            continue
            
        row, col = module_info['position']
        
        # Create subplot with 1x2 grid for this module (All Attempts + Successful Only)
        module_gs = gridspec.GridSpecFromSubplotSpec(1, 2, gs[row, col], wspace=0.3)
        
        chart_data = all_charts_data[module_key]
        
        # Get successful attempts data for second chart
        successful_data = []
        successful_labels = []
        successful_colors = []
        
        # Calculate successful attempts distribution
        report = all_charts_data[module_key]['report']
        successful_dist = report['comparative_statistics']['attempt_distribution']['successful']
        
        # Calculate 5-attempt successful only
        consolidated_5_success_only = 0
        for file_path, result in report['individual_results'].items():
            details = result['attempt_distribution']['by_attempt_details']
            if 5 in details:
                consolidated_5_success_only += len(details[5].get('successful', []))
        
        for attempts in sorted(successful_dist.keys()):
            if attempts == 5:
                if consolidated_5_success_only > 0:
                    successful_data.append(consolidated_5_success_only)
                    successful_labels.append(f'5\n({consolidated_5_success_only})')
                    successful_colors.append(attempt_colors['5_success'])
            else:
                total_count = successful_dist[attempts]
                successful_data.append(total_count)
                successful_labels.append(f'{attempts}\n({total_count})')
                successful_colors.append(attempt_colors[attempts])
        
        # Chart 1: All Attempts
        ax1 = fig.add_subplot(module_gs[0, 0])
        if chart_data['data']:
            wedges1, texts1, autotexts1 = ax1.pie(
                chart_data['data'], 
                labels=None,  # Remove labels to avoid crowding
                autopct='%1.1f%%',
                colors=chart_data['colors'], 
                startangle=90,
                pctdistance=0.8
            )
            
            # Style the percentage text
            for autotext in autotexts1:
                autotext.set_color('white')
                autotext.set_fontweight('bold')
                autotext.set_fontsize(14)
        
        # Adjust title padding based on module (Div Unit and Mul Unit need more space)
        title_pad = -110 if module_key in ['div_unit', 'mul_unit'] else -50
        ax1.set_title(f'All Correction Attempts', fontsize=16, fontweight='bold', pad=title_pad)
        ax1.axis('equal')
        
        # Chart 2: Successful Only
        ax2 = fig.add_subplot(module_gs[0, 1])
        if successful_data:
            wedges2, texts2, autotexts2 = ax2.pie(
                successful_data, 
                labels=None,  # Remove labels to avoid crowding
                autopct='%1.1f%%',
                colors=successful_colors, 
                startangle=90,
                pctdistance=0.8
            )
            
            # Style the percentage text
            for autotext in autotexts2:
                autotext.set_color('white')
                autotext.set_fontweight('bold')
                autotext.set_fontsize(14)
        else:
            ax2.text(0.5, 0.5, 'No Successful\nCorrections', ha='center', va='center', 
                    transform=ax2.transAxes, fontsize=14, fontweight='bold')
        
        ax2.set_title(f'Successful Corrections Only', fontsize=16, fontweight='bold', pad=title_pad)
        ax2.axis('equal')
        
        # Add module title above both charts
        fig.text((gs[row, col].get_position(fig).x0 + gs[row, col].get_position(fig).x1) / 2, 
                gs[row, col].get_position(fig).y1 + 0.01, 
                chart_data['title'], 
                ha='center', va='bottom', fontsize=24, fontweight='bold')
    
    # Add unified legend at the bottom
    legend_elements = [
        mpatches.Patch(color='#2ecc71', label='1 (success)'),
        mpatches.Patch(color='#3498db', label='2 (success)'),
        mpatches.Patch(color='#f39c12', label='3 (success)'),
        mpatches.Patch(color='#e74c3c', label='4 (success)'),
        mpatches.Patch(color='#9b59b6', label='5 (success)'),
        mpatches.Patch(color='#34495e', label='5 (failed)')
    ]
    
    # Add legend at the bottom
    fig.legend(handles=legend_elements, loc='lower center', 
              bbox_to_anchor=(0.5, 0.02), ncol=6, fontsize=20)
    
    # Save the unified figure
    output_file = f"{output_dir}/unified_semantic_correction_all_modules.png"
    plt.savefig(output_file, dpi=300, bbox_inches='tight', facecolor='white', 
               edgecolor='none', pad_inches=0.2)
    
    print(f"\n🎯 Unified semantic correction figure saved to: {output_file}")
    plt.close()

def main():
    parser = argparse.ArgumentParser(
        description="Generate unified semantic correction figure for all modules",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate unified figure for specific timestamp
  python3 src/analysis/generate_unified_semantic_correction.py --timestamp 20250823_014948
  
  # Save to custom directory
  python3 src/analysis/generate_unified_semantic_correction.py --timestamp 20250823_014948 --output-dir charts/
        """
    )
    
    parser.add_argument('--output-dir', '-o', default='charts',
                       help='Output directory for the figure (default: charts)')
    
    args = parser.parse_args()
    
    if not MATPLOTLIB_AVAILABLE:
        print("Error: matplotlib is required for this script.")
        sys.exit(1)
    
    generate_unified_semantic_correction_figure(args.output_dir)

if __name__ == "__main__":
    main() 