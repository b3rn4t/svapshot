#!/usr/bin/env python3
"""
Unified Property Set Extension Figure Generator

This script generates a single figure with 4 line plot panels showing property set extension
growth patterns for all modules (div_unit, mul_unit, i2c, ptw) in clockwise order.
The figure includes a single shared color legend at the bottom.

Author: AI Assistant
"""

import argparse
import sys
from pathlib import Path
from batch_analyze_property_set_extension import BatchPropertySetExtensionAnalyzer

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
    import matplotlib.colors as mcolors
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    print("Error: matplotlib is required for this script.")
    sys.exit(1)

def generate_unified_extension_figure(output_dir: str = "charts"):
    """Generate unified 2x2 property extension figure for all modules."""
    
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
    
    # Consistent LLM color scheme
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
    
    # Create figure with 2x2 subplots with better spacing
    fig = plt.figure(figsize=(20, 16))
    gs = gridspec.GridSpec(2, 2, figure=fig, hspace=0.4, wspace=0.5,
                          left=0.08, right=0.92, top=0.88, bottom=0.15)
    
    # Set main title with more vertical space
    fig.suptitle(f'Property Set Extension Growth Across All Modules',
                fontsize=28, fontweight='bold', y=0.96)
    
    # Track all LLM types for unified legend
    all_llm_types = set()
    
    # Process each module
    for module_key, module_info in modules.items():
        print(f"Processing {module_info['title']} module...")
        
        # Initialize analyzer for this module
        analyzer = BatchPropertySetExtensionAnalyzer()
        
        # Filter logs by the specific timestamp for this module
        base_dir = "svapshot_v1_results"
        module_timestamp = module_info['timestamp']
        file_paths = analyzer.filter_by_timestamp(base_dir, module_timestamp)
        
        # Filter experiments to only include this module
        filtered_experiments = {}
        for exp_name, exp_data in analyzer.experiments.items():
            # Special handling for PTW to include ptw, ptw_arb, and pseudoLRU
            if module_key == 'ptw':
                if any(sub in exp_data.rtl_module.lower() for sub in ['ptw', 'pseudolru']):
                    filtered_experiments[exp_name] = exp_data
                    all_llm_types.add(exp_data.llm_type)
            else:
                if module_key in exp_data.rtl_module.lower():
                    filtered_experiments[exp_name] = exp_data
                    all_llm_types.add(exp_data.llm_type)
        
        if not filtered_experiments:
            print(f"  No data found for {module_info['title']}")
            continue
        
        # Create subplot
        row, col = module_info['position']
        ax = fig.add_subplot(gs[row, col])
        
        # Plot each experiment for this module
        for exp_name, exp_data in filtered_experiments.items():
            if not exp_data.iterations:
                continue
            
            # Prepare data for plotting
            iterations = [0] + [iter.iteration_number for iter in exp_data.iterations]
            initial_props = (exp_data.iterations[0].cumulative_properties - 
                           exp_data.iterations[0].properties_added if exp_data.iterations else 0)
            cumulative_props = [initial_props] + [iter.cumulative_properties for iter in exp_data.iterations]
            
            # Get consistent color for this LLM type
            color = llm_colors.get(exp_data.llm_type, 
                                 llm_colors.get(exp_data.llm_type.split('_')[-1], '#95a5a6'))
            
            # Create clean sub-module name and LLM label (remove timestamps and hl_ prefix)
            clean_module = exp_data.rtl_module
            # Remove timestamp patterns (YYYYMMDD_HHMMSS format)
            import re
            clean_module = re.sub(r'_\d{8}_\d{6}', '', clean_module)
            # Remove any remaining timestamp-like patterns
            clean_module = re.sub(r'_20250\d+', '', clean_module)
            # Remove "hl_" prefix for high-level modules
            clean_module = re.sub(r'^hl_', '', clean_module)
            llm_short = exp_data.llm_type.replace('_', '-').upper()
            
            # Plot line without label (we'll add text annotations instead) - thicker lines and larger markers
            ax.plot(iterations, cumulative_props, 
                   marker='o', linewidth=4, markersize=10,
                   color=color)
            
            # Add text annotation at the end of the curve with sub-module name
            if iterations and cumulative_props:
                final_x = iterations[-1]
                final_y = cumulative_props[-1]
                ax.annotate(f'{clean_module}', 
                          (final_x, final_y),
                          xytext=(8, 0), textcoords='offset points',
                          va='center', ha='left', fontsize=14, fontweight='bold',
                          color=color)
            
            # Add value annotations for property additions
            for i, (x, y) in enumerate(zip(iterations[1:], cumulative_props[1:])):
                added = exp_data.iterations[i].properties_added
                ax.annotate(f'+{added}', (x, y), 
                          textcoords="offset points", xytext=(0,6), 
                          ha='center', fontsize=12, color=color, fontweight='bold', alpha=0.9)
        
        # Customize subplot
        ax.set_title(module_info['title'], fontsize=22, fontweight='bold', pad=25)
        ax.set_xlabel('Extension Iteration', fontsize=18, fontweight='bold')
        ax.set_ylabel('Cumulative Properties', fontsize=18, fontweight='bold')
        
        # Set integer ticks for x-axis
        if filtered_experiments:
            max_iterations = max(len(exp_data.iterations) for exp_data in filtered_experiments.values())
            ax.set_xticks(range(max_iterations + 1))
        
        # Add grid
        ax.grid(True, alpha=0.3, linestyle='--')
    
    # Create unified legend at the bottom for all LLM types
    legend_elements = []
    for llm_type in sorted(all_llm_types):
        color = llm_colors.get(llm_type, 
                             llm_colors.get(llm_type.split('_')[-1], '#95a5a6'))
        
        # Format LLM names for better readability
        if llm_type == 'gpt_41_mini':
            label = 'GPT-4.1-MINI'
        elif llm_type == 'llama_33_70b':
            label = 'LLAMA-3.3-70B'
        elif llm_type == 'llama_405b':
            label = 'LLAMA-405B'
        else:
            # Fallback for any other LLM types
            label = llm_type.replace('_', '-').upper()
        
        legend_elements.append(mpatches.Patch(color=color, label=label))
    
    # Add unified legend at the bottom
    if legend_elements:
        fig.legend(handles=legend_elements, loc='lower center', 
                  bbox_to_anchor=(0.5, 0.05), ncol=len(legend_elements), fontsize=18)
    
    # Save the unified figure
    output_file = f"{output_dir}/unified_property_extension_all_modules.png"
    plt.savefig(output_file, dpi=300, bbox_inches='tight', facecolor='white', 
               edgecolor='none', pad_inches=0.2)
    
    print(f"\n🎯 Unified property extension figure saved to: {output_file}")
    plt.close()

def main():
    parser = argparse.ArgumentParser(
        description="Generate unified property set extension figure for all modules",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate unified figure for specific timestamp
  python3 src/analysis/generate_unified_property_extension.py --timestamp 20250823_014948
  
  # Save to custom directory
  python3 src/analysis/generate_unified_property_extension.py --timestamp 20250823_014948 --output-dir charts/
        """
    )
    
    parser.add_argument('--output-dir', '-o', default='charts',
                       help='Output directory for the figure (default: charts)')
    
    args = parser.parse_args()
    
    if not MATPLOTLIB_AVAILABLE:
        print("Error: matplotlib is required for this script.")
        sys.exit(1)
    
    generate_unified_extension_figure(args.output_dir)

if __name__ == "__main__":
    main() 