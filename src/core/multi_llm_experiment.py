#!/usr/bin/env python3

import os
import sys
import time
import subprocess
import argparse
import shutil
import re
from datetime import datetime

CORE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(CORE_DIR))
FPV_SCRIPTS_DIR = os.path.join(REPO_ROOT, 'fpv_app_scripts')
UTILITY_SCRIPTS_DIR = os.path.join(REPO_ROOT, 'scripts')

# Logging functionality
class ExperimentLogger:
    """Logger class for multi-LLM experiments with file and console output."""

    def __init__(self, log_dir=".", experiment_name=None):
        """Initialize the logger.

        Args:
            log_dir (str): Directory to store log files
            experiment_name (str): Optional experiment name for log file naming
        """
        self.log_dir = log_dir

        # Create logs directory if it doesn't exist
        os.makedirs(log_dir, exist_ok=True)

        # Create log file name
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if experiment_name:
            log_filename = f"multi_llm_experiment_{experiment_name}_{timestamp}.log"
        else:
            log_filename = f"multi_llm_experiment_{timestamp}.log"

        self.log_file_path = os.path.join(log_dir, log_filename)

        # Initialize log file
        try:
            with open(self.log_file_path, 'w') as log_file:
                log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Multi-LLM Experiment Log Started\n")
                log_file.write("=" * 80 + "\n")
        except Exception as e:
            print(f"Warning: Failed to initialize log file: {e}")
            self.log_file_path = None

    def log(self, message, level="INFO"):
        """Log a message to both console and file.

        Args:
            message (str): Message to log
            level (str): Log level (INFO, DEBUG, WARNING, ERROR)
        """
        # Print to console (preserve original behavior)
        print(message)

        # Write to log file
        self._write_to_log_file(message, level)

    def _write_to_log_file(self, message, level):
        """Write message to log file with timestamp and level.

        Args:
            message (str): Message to write
            level (str): Log level
        """
        if not self.log_file_path:
            return

        try:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with open(self.log_file_path, 'a') as log_file:
                log_file.write(f"{message}\n")
        except Exception as e:
            print(f"Warning: Failed to write to log file: {e}")

    def log_subprocess_output(self, cmd, result, success=True):
        """Log subprocess execution details.

        Args:
            cmd (list): Command that was executed
            result: subprocess result object
            success (bool): Whether the command was successful
        """
        level = "INFO" if success else "ERROR"
        self.log(f"Command executed: {' '.join(cmd)}", level)

        if hasattr(result, 'stdout') and result.stdout:
            self.log(f"STDOUT: {result.stdout}", level)
        if hasattr(result, 'stderr') and result.stderr:
            self.log(f"STDERR: {result.stderr}", level)
        if hasattr(result, 'returncode'):
            self.log(f"Return code: {result.returncode}", level)

    def finalize_log(self):
        """Write final log entry."""
        if self.log_file_path:
            try:
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                with open(self.log_file_path, 'a') as log_file:
                    log_file.write(f"[{timestamp}] [INFO] Multi-LLM Experiment completed\n")
                    log_file.write("=" * 80 + "\n")
                print(f"📄 Full experiment log saved to: {self.log_file_path}")
            except Exception as e:
                print(f"Warning: Failed to finalize log: {e}")

def parse_arguments():
    """Parse command line arguments for multi-LLM experiments."""
    epilog = """
Examples:
  Basic experiment with 3 models:
    python3 src/core/multi_llm_experiment.py hl_mul_unit.sv sequential golden medium

  With coverage analysis for all versions:
    python3 src/core/multi_llm_experiment.py hl_mul_unit.sv sequential golden medium --run-coverage

  Coverage analysis for specific versions only:
    python3 src/core/multi_llm_experiment.py hl_mul_unit.sv sequential golden medium --run-coverage --coverage-versions i f

  Custom models and experiment name:
    python3 src/core/multi_llm_experiment.py hl_mul_unit.sv sequential golden medium \\
        --llm-models "gpt-4.1-mini" "meta/llama-3.3-70b-instruct" "anthropic/claude-3.5-sonnet" \\
        --experiment-name "comparison_test" --run-coverage

Coverage Version Codes:
  i = Initial properties (before any LLM improvements)
  s = After syntax correction phase
  e = After extension/refinement phase
  c = After semantic correction phase
  f = Final properties (after all improvements)
"""

    parser = argparse.ArgumentParser(
        description='AI-SVA Multi-LLM Experiment Runner: Execute the same RTL module with 3 different LLM models',
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument('rtl_module', nargs='?',
                       help='RTL source module (e.g., hl_mul_unit.sv)')

    parser.add_argument('module_type', nargs='?',
                       choices=['sequential', 'combinational'],
                       help='Type of RTL module')

    parser.add_argument('execution_type', nargs='?',
                       choices=['golden', 'validation'],
                       help='Type of execution (golden includes semantic correction, validation does not)')

    parser.add_argument('verbosity', nargs='?',
                       choices=['low', 'medium', 'high', 'debug'],
                       help='Verbosity level (low, medium, high, debug)')

    parser.add_argument('--sources', '-src',
                       nargs='+',
                       default=['sources'],
                       help='Source directories (default: sources)')

    parser.add_argument('--includes', '-i',
                       default='packages',
                       help='Include directory (default: packages)')

    parser.add_argument('--assertion-source', '-as',
                       choices=['rtl', 'file'],
                       default='rtl',
                       help='Source of initial assertions: "rtl" to prompt with RTL and SVA rules, or "file" to read from an initial file (default: rtl)')

    parser.add_argument('--initial-file', '-if',
                       default='initial_assertions',
                       help='File containing initial assertions when using --assertion-source file (default: initial_assertions)')

    parser.add_argument('--disable-package-detection', '-dpd',
                       action='store_true',
                       help='Disable automatic package detection for manual_sub.vc files')

    parser.add_argument('--llm-models', '-llm',
                       nargs=3,
                       default=[
                           'meta/llama-3.3-70b-instruct',
                           'gpt-4.1-mini',
                           'meta/llama-3.1-405b-instruct'
                       ],
                       help='Three LLM models to use for experiments (default: llama-3.3-70b, gpt-4.1-mini, llama-3.1-405b)')

    parser.add_argument('--experiment-name', '-en',
                       help='Optional experiment name to include in folder naming (default: timestamp)')

    parser.add_argument('--keep-intermediate', '-ki',
                       action='store_true',
                       help='Keep intermediate files and folders from each experiment')

    parser.add_argument('--run-coverage', '-rc',
                       action='store_true',
                       help='Run JasperGold coverage analysis after experiments complete')

    parser.add_argument('--coverage-versions', '-cv',
                       nargs='+',
                       choices=['i', 's', 'e', 'c', 'f'],
                       default=['i', 's', 'e', 'c', 'f'],
                       help='Property versions to analyze for coverage (i=initial, s=after_syntax, e=after_extension, c=after_semantic_correction, f=final)')

    return parser.parse_args()

def prompt_for_missing_args(args):
    """Prompt user for any missing required arguments."""
    print("🔧 Checking for missing parameters...")

    # RTL module
    if not args.rtl_module:
        while True:
            rtl_module = input("\n📄 Enter RTL module path (e.g., hl_mul_unit.sv): ").strip()
            if rtl_module:
                args.rtl_module = rtl_module
                break
            print("❌ RTL module path is required!")

    # Module type
    if not args.module_type:
        while True:
            print("\n🔧 Module type options:")
            print("   1. sequential")
            print("   2. combinational")
            choice = input("Select module type (1-2): ").strip()
            if choice == '1':
                args.module_type = 'sequential'
                break
            elif choice == '2':
                args.module_type = 'combinational'
                break
            print("❌ Please select 1 or 2!")

    # Execution type
    if not args.execution_type:
        while True:
            print("\n🚀 Execution type options:")
            print("   1. golden (includes semantic correction)")
            print("   2. validation (no semantic correction)")
            choice = input("Select execution type (1-2): ").strip()
            if choice == '1':
                args.execution_type = 'golden'
                break
            elif choice == '2':
                args.execution_type = 'validation'
                break
            print("❌ Please select 1 or 2!")

    # Verbosity
    if not args.verbosity:
        while True:
            print("\n📢 Verbosity level options:")
            print("   1. low")
            print("   2. medium")
            print("   3. high")
            print("   4. debug")
            choice = input("Select verbosity level (1-4): ").strip()
            if choice == '1':
                args.verbosity = 'low'
                break
            elif choice == '2':
                args.verbosity = 'medium'
                break
            elif choice == '3':
                args.verbosity = 'high'
                break
            elif choice == '4':
                args.verbosity = 'debug'
                break
            print("❌ Please select 1-4!")

    return args

def sanitize_model_name(model_name):
    """Convert LLM model name to a filesystem-safe string."""
    # Replace common characters that might cause issues in filenames
    sanitized = model_name.replace('/', '_').replace('\\', '_')
    sanitized = sanitized.replace(':', '_').replace(' ', '_')
    sanitized = sanitized.replace('-', '_').replace('.', '_')
    sanitized = sanitized.lower()

    # Shorten common model names for readability
    replacements = {
        'meta_llama_3_3_70b_instruct': 'llama_33_70b',
        'gpt_4_1_mini': 'gpt_41_mini',
        'meta_llama_3_1_405b_instruct': 'llama_405b',
        'anthropic_claude_3_5_sonnet': 'claude_35_sonnet',
        'openai_gpt_4o': 'gpt_4o',
        'meta_llama_3_1_70b_instruct': 'llama_70b',
        'meta_llama_3_1_8b_instruct': 'llama_8b',
        'anthropic_claude_3_haiku': 'claude_3_haiku',
        'openai_gpt_4_turbo': 'gpt_4_turbo',
        'openai_gpt_3_5_turbo': 'gpt_35_turbo'
    }

    for full_name, short_name in replacements.items():
        if sanitized == full_name:
            return short_name

    # If no replacement found, return the sanitized version (truncated if too long)
    if len(sanitized) > 30:
        sanitized = sanitized[:30]

    return sanitized

def run_experiment(args, llm_model, experiment_num, total_experiments):
    """Run a single experiment with the specified LLM model."""
    print(f"\n{'='*80}")
    print(f"🧪 EXPERIMENT {experiment_num}/{total_experiments}: {llm_model}")
    print(f"{'='*80}")

    # Build command for main.py
    cmd = [
        sys.executable, os.path.join(CORE_DIR, 'main.py'),
        args.rtl_module,
        llm_model,
        args.module_type,
        args.execution_type,
        args.verbosity
    ]

    # Add optional arguments
    if args.sources != ['sources']:
        cmd.extend(['--sources'] + args.sources)

    if args.includes != 'packages':
        cmd.extend(['--includes', args.includes])

    if args.assertion_source != 'rtl':
        cmd.extend(['--assertion-source', args.assertion_source])

    if args.initial_file != 'initial_assertions':
        cmd.extend(['--initial-file', args.initial_file])

    if args.disable_package_detection:
        cmd.append('--disable-package-detection')

    print(f"🚀 Executing: {' '.join(cmd)}")
    print(f"⏱️  Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    start_time = time.time()

    try:
        # Run the experiment
        result = subprocess.run(cmd, check=True)

        end_time = time.time()
        duration = end_time - start_time

        print(f"✅ Experiment {experiment_num} completed successfully!")
        print(f"⏱️  Duration: {duration:.2f} seconds")

        return True, duration

    except subprocess.CalledProcessError as e:
        end_time = time.time()
        duration = end_time - start_time

        print(f"❌ Experiment {experiment_num} failed with return code {e.returncode}")
        print(f"⏱️  Duration before failure: {duration:.2f} seconds")

        return False, duration
    except KeyboardInterrupt:
        print(f"\n🛑 Experiment {experiment_num} interrupted by user")
        return False, time.time() - start_time

def discover_testbench_folders():
    """Discover fresh testbench folders that start with 'ft_' but are not already renamed.

    Returns:
        list: List of fresh (unrenamed) testbench folder names
    """
    testbench_folders = []
    for item in os.listdir('.'):
        if os.path.isdir(item) and item.startswith('ft_'):
            # Skip folders that already have experiment names or model suffixes
            # (indicating they've been renamed from a previous experiment)
            if '_20' not in item and '_llama' not in item and '_gpt' not in item and '_claude' not in item:
                testbench_folders.append(item)
    return sorted(testbench_folders)

def rename_testbench_folders(rtl_module, llm_model, experiment_name=None):
    """Rename all testbench folders (main module + submodules) to include the LLM model name.

    Args:
        rtl_module (str): RTL module path
        llm_model (str): LLM model name
        experiment_name (str): Optional experiment name

    Returns:
        list: List of renamed folder names, or empty list if no folders found
    """
    # Extract module name from RTL file
    module_name = os.path.splitext(os.path.basename(rtl_module))[0]
    main_folder = f'ft_{module_name}'

    # Discover all testbench folders
    testbench_folders = discover_testbench_folders()

    if not testbench_folders:
        print(f"⚠️  No testbench folders found starting with 'ft_'")
        return []

    print(f"🔍 Found {len(testbench_folders)} testbench folder(s): {', '.join(testbench_folders)}")

    # Check if main module folder exists
    if main_folder not in testbench_folders:
        print(f"⚠️  Main testbench folder not found: {main_folder}")
        return []

    # Create model suffix
    model_suffix = sanitize_model_name(llm_model)
    renamed_folders = []

    # Rename all discovered testbench folders
    for original_folder in testbench_folders:
        try:
            # Create new folder name
            if experiment_name:
                new_folder = f'{original_folder}_{experiment_name}_{model_suffix}'
            else:
                new_folder = f'{original_folder}_{model_suffix}'

            # Handle existing folders with same name
            counter = 1
            base_new_folder = new_folder
            while os.path.exists(new_folder):
                new_folder = f"{base_new_folder}_{counter}"
                counter += 1

            # Rename the folder
            shutil.move(original_folder, new_folder)
            print(f"📁 Renamed testbench folder: {original_folder} → {new_folder}")
            renamed_folders.append(new_folder)

        except Exception as e:
            print(f"❌ Failed to rename folder {original_folder}: {str(e)}")

    if renamed_folders:
        print(f"✅ Successfully renamed {len(renamed_folders)} testbench folder(s)")
        if len(renamed_folders) > 1:
            print(f"📋 Preserved submodule testbenches:")
            for folder in renamed_folders:
                if not folder.startswith(f'ft_{module_name}_'):
                    print(f"   • {folder}")

    return renamed_folders

def rename_testbench_folder(rtl_module, llm_model, experiment_name=None):
    """Legacy function - redirects to new multi-folder rename function.

    Maintained for backward compatibility, but now handles all testbench folders.

    Returns:
        str: Main module folder name, or None if failed
    """
    renamed_folders = rename_testbench_folders(rtl_module, llm_model, experiment_name)

    if not renamed_folders:
        return None

    # Return the main module folder (should be first in the list after sorting)
    module_name = os.path.splitext(os.path.basename(rtl_module))[0]
    main_folder_prefix = f'ft_{module_name}_'

    for folder in renamed_folders:
        if folder.startswith(main_folder_prefix):
            return folder

    # Fallback: return the first renamed folder
    return renamed_folders[0]

def cleanup_intermediate_files(keep_intermediate):
    """Clean up intermediate files if not keeping them."""
    if keep_intermediate:
        print("🗂️  Keeping all intermediate files as requested")
        return

    # List of files to clean up
    cleanup_files = [
        'initial_assertions'
    ]

    cleaned_count = 0
    for file_path in cleanup_files:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                cleaned_count += 1
                print(f"🗑️  Removed: {file_path}")
            except Exception as e:
                print(f"⚠️  Could not remove {file_path}: {str(e)}")

    if cleaned_count > 0:
        print(f"🧹 Cleaned up {cleaned_count} intermediate files")
    else:
        print("🧹 No intermediate files to clean up")

def find_rtl_module_file(rtl_module, sources, includes):
    """Find the RTL module file if not in current directory.

    This function mirrors the logic from main.py to ensure consistent
    path resolution across all scripts.

    Args:
        rtl_module (str): RTL module name or path
        sources (list): List of source directories
        includes (str): Include directory path

    Returns:
        str: Full path to RTL module file, or original rtl_module if not found
    """
    # If it already exists as specified, return it
    if os.path.exists(rtl_module):
        return rtl_module

    # Build search directories
    search_dirs = list(sources) if sources else ['sources']
    if includes and includes not in search_dirs:
        search_dirs.append(includes)

    print(f"🔍 RTL module '{rtl_module}' not found in current directory.")
    print(f"🔍 Searching in source directories: {search_dirs}")

    # Extract just the filename if a path was provided
    module_filename = os.path.basename(rtl_module)

    # Try different search strategies
    search_patterns = [
        rtl_module,           # Original name (could be a relative path)
        module_filename,      # Just the filename
    ]

    for search_dir in search_dirs:
        if not os.path.exists(search_dir):
            continue

        for pattern in search_patterns:
            # Try direct path in search directory
            candidate_path = os.path.join(search_dir, pattern)
            if os.path.exists(candidate_path):
                print(f"✅ Found RTL module at: {candidate_path}")
                return candidate_path

            # Try searching recursively for the file
            for root, dirs, files in os.walk(search_dir):
                if pattern in files:
                    candidate_path = os.path.join(root, pattern)
                    print(f"✅ Found RTL module at: {candidate_path}")
                    return candidate_path

    # If not found, return original and let the error handling deal with it
    print(f"❌ Could not locate '{rtl_module}' in source directories.")
    return rtl_module

def extract_dut_root_from_experiment(experiment_folder, rtl_module):
    """Extract DUT_ROOT environment variable from experiment's agent log.

    Args:
        experiment_folder (str): Path to experiment folder
        rtl_module (str): RTL module name

    Returns:
        str: DUT_ROOT value if found, None otherwise
    """
    try:
        sva_dir = os.path.join(experiment_folder, 'sva')
        if not os.path.exists(sva_dir):
            print(f"⚠️  SVA directory not found in {experiment_folder}")
            return None

        # Find the most recent agent log file
        agent_logs = []
        for filename in os.listdir(sva_dir):
            if filename.startswith('agent_log_') and filename.endswith('.log'):
                agent_logs.append(os.path.join(sva_dir, filename))

        if not agent_logs:
            print(f"⚠️  No agent log files found in {sva_dir}")
            return None

        # Use the most recent log file
        agent_log = max(agent_logs, key=os.path.getmtime)
        print(f"🔍 Checking agent log: {agent_log}")

        # Read the log file and look for RTL source line
        with open(agent_log, 'r') as f:
            content = f.read()

        # Look for "RTL source: " pattern
        rtl_source_match = re.search(r'RTL source:\s*(.+)', content)
        if not rtl_source_match:
            print(f"⚠️  RTL source line not found in agent log")
            return None

        rtl_source_path = rtl_source_match.group(1).strip()
        print(f"📍 Found RTL source path: {rtl_source_path}")

        # Also check files.vc for relative path pattern
        files_vc = os.path.join(experiment_folder, 'files.vc')
        if os.path.exists(files_vc):
            with open(files_vc, 'r') as f:
                files_content = f.read()

            # Look for ${DUT_ROOT}/... pattern containing the module
            module_basename = os.path.basename(rtl_module)
            if module_basename.endswith('.sv'):
                module_basename = module_basename[:-3]
                file_pattern = rf'\${{DUT_ROOT}}/.*/{re.escape(module_basename)}\.sv'
            elif module_basename.endswith('.v'):
                module_basename = module_basename[:-2]
                file_pattern = rf'\${{DUT_ROOT}}/.*/{re.escape(module_basename)}\.v'
            else:
                # Fallback: search for both extensions
                file_pattern = rf'\${{DUT_ROOT}}/.*/{re.escape(module_basename)}\.(?:sv|v)'
            dut_root_pattern = file_pattern
            dut_root_match = re.search(dut_root_pattern, files_content)

            if dut_root_match:
                relative_path = dut_root_match.group(0).replace('${DUT_ROOT}/', '')
                print(f"📍 Found relative path in files.vc: {relative_path}")

                # Extract DUT_ROOT by removing relative path from absolute path
                if rtl_source_path.endswith(relative_path):
                    dut_root = rtl_source_path[:-len(relative_path)].rstrip('/')
                    print(f"✅ Extracted DUT_ROOT: {dut_root}")
                    return dut_root

        # Fallback: use directory containing the RTL module
        dut_root = os.path.dirname(rtl_source_path)
        print(f"✅ Using fallback DUT_ROOT: {dut_root}")
        return dut_root

    except Exception as e:
        print(f"❌ Error extracting DUT_ROOT from {experiment_folder}: {str(e)}")
        return None

def copy_experiment_to_base_folder(experiment_folder, rtl_module):
    """Copy experiment folder to base testbench folder name.

    Args:
        experiment_folder (str): Source experiment folder
        rtl_module (str): RTL module path

    Returns:
        str: Path to copied base folder, None if failed
    """
    try:
        # Extract module name from RTL module path
        module_name = os.path.splitext(os.path.basename(rtl_module))[0]
        base_folder = f'ft_{module_name}'

        # Check if experiment folder exists
        if not os.path.exists(experiment_folder):
            print(f"❌ Experiment folder not found: {experiment_folder}")
            return None

        # Backup existing base folder if present
        backup_folder = None
        if os.path.exists(base_folder):
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_folder = f'{base_folder}_backup_{timestamp}'
            counter = 1
            while os.path.exists(backup_folder):
                backup_folder = f'{base_folder}_backup_{timestamp}_{counter}'
                counter += 1

            print(f"📁 Backing up existing folder: {base_folder} → {backup_folder}")
            shutil.move(base_folder, backup_folder)

        # Copy experiment folder to base folder name
        print(f"📁 Copying experiment folder: {experiment_folder} → {base_folder}")
        shutil.copytree(experiment_folder, base_folder)

        return base_folder, backup_folder

    except Exception as e:
        print(f"❌ Error copying experiment folder: {str(e)}")
        return None, None

def restore_base_folder(base_folder, backup_folder, original_folder):
    """Restore folders to original state after coverage analysis.

    Args:
        base_folder (str): Base folder to remove
        backup_folder (str): Backup folder to restore (if any)
        original_folder (str): Original experiment folder to restore
    """
    try:
        # Remove the temporary base folder
        if os.path.exists(base_folder):
            shutil.rmtree(base_folder)
            print(f"🗑️  Removed temporary folder: {base_folder}")

        # Restore the original experiment folder name
        if os.path.exists(original_folder + '_temp'):
            shutil.move(original_folder + '_temp', original_folder)
            print(f"📁 Restored original folder: {original_folder}")

        # Restore backup if it exists
        if backup_folder and os.path.exists(backup_folder):
            shutil.move(backup_folder, base_folder)
            print(f"📁 Restored backup folder: {backup_folder} → {base_folder}")

    except Exception as e:
        print(f"⚠️  Error during folder restoration: {str(e)}")

def get_property_versions(base_folder, module_name):
    """Get available property file versions for coverage analysis.

    Args:
        base_folder (str): Base testbench folder
        module_name (str): Module name

    Returns:
        list: List of available version suffixes
    """
    versions_dir = os.path.join(base_folder, 'sva', 'versions')
    if not os.path.exists(versions_dir):
        print(f"⚠️  Versions directory not found: {versions_dir}")
        return []

    available_versions = []
    version_patterns = {
        'i': f'{module_name}_prop_initial.sv',
        's': f'{module_name}_prop_after_syntax.sv',
        'e': f'{module_name}_prop_after_extension.sv',
        'c': f'{module_name}_prop_after_semantic_correction.sv',
        'f': 'final.sv'
    }

    for suffix, filename in version_patterns.items():
        version_file = os.path.join(versions_dir, filename)
        if os.path.exists(version_file):
            available_versions.append(suffix)
            print(f"✅ Found version '{suffix}': {filename}")

    return available_versions

def run_coverage_analysis(base_folder, module_name, dut_root, version_suffix):
    """Run JasperGold coverage analysis for a specific property version.

    Args:
        base_folder (str): Base testbench folder
        module_name (str): Module name
        dut_root (str): DUT_ROOT environment variable value
        version_suffix (str): Property version suffix (i, s, e, f)

    Returns:
        dict: Coverage results or None if failed
    """
    try:
        print(f"\n🔬 Running coverage analysis for version '{version_suffix}'...")

        # Set DUT_ROOT environment variable
        original_dut_root = os.environ.get('DUT_ROOT')
        os.environ['DUT_ROOT'] = dut_root
        print(f"🔧 Set DUT_ROOT={dut_root}")

        # Copy the desired property version to the main property file
        copy_cmd = [
            os.path.join(UTILITY_SCRIPTS_DIR, 'copy_prop.sh'),
            module_name,
            version_suffix,
        ]
        print(f"📋 Executing: {' '.join(copy_cmd)}")

        result = subprocess.run(copy_cmd, check=True, capture_output=True, text=True)
        if result.stdout:
            print(f"📄 {result.stdout.strip()}")

        # Clean up previous JasperGold results
        projs_dir = f'projs/{module_name}'
        if os.path.exists(projs_dir):
            shutil.rmtree(projs_dir)
            print(f"🗑️  Cleaned up previous results: {projs_dir}")

        # Run JasperGold in batch mode
        jg_cmd = [
            os.path.join(FPV_SCRIPTS_DIR, 'run_jg_batch.sh'),
            module_name,
        ]
        print(f"🚀 Executing: {' '.join(jg_cmd)}")

        start_time = time.time()
        # Detached from any terminal, or a read from one stops the tool tree
        # with SIGTTIN part-way through the proof.
        jg_result = subprocess.run(jg_cmd, check=True, capture_output=True, text=True,
                                   stdin=subprocess.DEVNULL, start_new_session=True)

        # Wait for log file to be generated
        log_file = os.path.join(projs_dir, 'jg.log')
        wait_count = 0
        max_wait = 60  # Wait up to 60 seconds

        while not os.path.exists(log_file) and wait_count < max_wait:
            time.sleep(1)
            wait_count += 1
            if wait_count % 10 == 0:
                print(f"⏳ Waiting for JasperGold log file... ({wait_count}s)")

        if not os.path.exists(log_file):
            print(f"❌ JasperGold log file not found after {max_wait}s: {log_file}")
            return None

        # Parse coverage from log file
        with open(log_file, 'r') as f:
            log_content = f.read()

        # Extract checker coverage using improved pattern that targets the specific module
        # Pattern to match coverage table rows with module names
        coverage_line_pattern = rf'\|\|{re.escape(module_name)}\s*\|\s*[\d.]+%\s*\(\d+/\d+\)\s*\|\s*[\d.]+%\s*\(\d+/\d+\)\s*\|\s*([\d.]+%)'

        # Find all matches for the specific module
        matches = re.findall(coverage_line_pattern, log_content)

        if not matches:
            # Fallback: try a more general pattern that looks for any module name in the coverage table
            general_pattern = r'\|\|[\w\-_]+\s*\|\s*[\d.]+%\s*\(\d+/\d+\)\s*\|\s*[\d.]+%\s*\(\d+/\d+\)\s*\|\s*([\d.]+%)'
            all_matches = re.findall(general_pattern, log_content)
            if all_matches:
                # Take the first match as fallback
                matches = [all_matches[0]]
                print(f"⚠️  Used fallback coverage pattern for {module_name}, found {len(all_matches)} total matches")

        coverage_result = {
            'version': version_suffix,
            'duration': time.time() - start_time,
            'coverage': None,
            'log_file': log_file
        }

        if matches:
            coverage_value = matches[0]
            coverage_result['coverage'] = float(coverage_value.replace('%', ''))
            print(f"✅ Coverage analysis completed: {coverage_value}")
        else:
            print(f"⚠️  Could not extract coverage from log file")
            # Save a portion of the log for debugging
            coverage_result['log_snippet'] = log_content[-1000:] if len(log_content) > 1000 else log_content

        return coverage_result

    except subprocess.CalledProcessError as e:
        print(f"❌ Coverage analysis failed: {e}")
        print(f"STDOUT: {e.stdout}")
        print(f"STDERR: {e.stderr}")
        return None
    except Exception as e:
        print(f"❌ Error during coverage analysis: {str(e)}")
        return None
    finally:
        # Restore original DUT_ROOT
        if original_dut_root is not None:
            os.environ['DUT_ROOT'] = original_dut_root
        elif 'DUT_ROOT' in os.environ:
            del os.environ['DUT_ROOT']

def run_post_experiment_coverage_analysis(results, rtl_module, requested_versions=None):
    """Run comprehensive coverage analysis for all successful experiments.

    Args:
        results (list): List of experiment results
        rtl_module (str): RTL module path
        requested_versions (list): List of property versions to analyze (default: all available)

    Returns:
        dict: Coverage analysis results for all experiments
    """
    print(f"\n{'='*80}")
    print(f"🔬 POST-EXPERIMENT COVERAGE ANALYSIS")
    print(f"{'='*80}")

    module_name = os.path.splitext(os.path.basename(rtl_module))[0]
    coverage_results = {}

    # Filter successful experiments
    successful_experiments = [r for r in results if r['success'] and r['folder']]

    if not successful_experiments:
        print(f"❌ No successful experiments found for coverage analysis")
        return coverage_results

    print(f"📊 Analyzing coverage for {len(successful_experiments)} successful experiments")

    for i, experiment in enumerate(successful_experiments, 1):
        experiment_folder = experiment['folder']
        model_name = sanitize_model_name(experiment['model'])

        print(f"\n{'-'*60}")
        print(f"🧪 COVERAGE ANALYSIS {i}/{len(successful_experiments)}: {model_name}")
        print(f"📁 Experiment folder: {experiment_folder}")
        print(f"{'-'*60}")

        # Extract DUT_ROOT from experiment
        dut_root = extract_dut_root_from_experiment(experiment_folder, rtl_module)
        if not dut_root:
            print(f"❌ Could not extract DUT_ROOT from {experiment_folder}")
            continue

        # Copy experiment to base folder
        base_folder, backup_folder = copy_experiment_to_base_folder(experiment_folder, rtl_module)
        if not base_folder:
            continue

        # Temporarily rename original experiment folder to avoid conflicts
        temp_experiment_folder = experiment_folder + '_temp'
        if os.path.exists(experiment_folder):
            shutil.move(experiment_folder, temp_experiment_folder)

        try:
            # Get available property versions
            available_versions = get_property_versions(base_folder, module_name)
            if not available_versions:
                print(f"⚠️  No property versions found for {experiment_folder}")
                continue

            # Filter versions based on user request
            if requested_versions:
                versions_to_analyze = [v for v in requested_versions if v in available_versions]
                if not versions_to_analyze:
                    print(f"⚠️  None of the requested versions {requested_versions} are available in {experiment_folder}")
                    continue
            else:
                versions_to_analyze = available_versions

            print(f"📊 Will analyze versions: {versions_to_analyze}")

            # Initialize results for this experiment
            experiment_coverage = {
                'model': experiment['model'],
                'folder': experiment_folder,
                'dut_root': dut_root,
                'versions': {}
            }

            # Run coverage analysis for each requested version
            for version in versions_to_analyze:
                version_result = run_coverage_analysis(base_folder, module_name, dut_root, version)
                if version_result:
                    experiment_coverage['versions'][version] = version_result

            coverage_results[model_name] = experiment_coverage

            # Summary for this experiment
            print(f"\n📊 Coverage Summary for {model_name}:")
            for version, result in experiment_coverage['versions'].items():
                version_names = {'i': 'Initial', 's': 'After Syntax', 'e': 'After Extension', 'c': 'After Semantic Correction', 'f': 'Final'}
                version_name = version_names.get(version, version)

                if result['coverage'] is not None:
                    print(f"   {version_name:20}: {result['coverage']:6.1f}% ({result['duration']:.1f}s)")
                else:
                    print(f"   {version_name:20}: FAILED ({result['duration']:.1f}s)")

        finally:
            # Restore folder structure
            restore_base_folder(base_folder, backup_folder, experiment_folder)

    return coverage_results

def print_coverage_analysis_summary(coverage_results):
    """Print a comprehensive summary of coverage analysis results.

    Args:
        coverage_results (dict): Coverage results from all experiments
    """
    if not coverage_results:
        print(f"\n❌ No coverage results to display")
        return

    print(f"\n{'='*80}")
    print(f"📊 COMPREHENSIVE COVERAGE ANALYSIS SUMMARY")
    print(f"{'='*80}")

    # Collect all available versions across experiments
    all_versions = set()
    for experiment in coverage_results.values():
        all_versions.update(experiment['versions'].keys())

    all_versions = sorted(all_versions)
    version_names = {'i': 'Initial', 's': 'After Syntax', 'e': 'After Extension', 'c': 'After Semantic Correction', 'f': 'Final'}

    # Print table header
    header = f"{'Model':<20}"
    for version in all_versions:
        version_name = version_names.get(version, version)
        header += f" | {version_name:>15}"
    print(header)
    print("-" * len(header))

    # Print results for each model
    for model_name, experiment in coverage_results.items():
        row = f"{model_name:<20}"
        for version in all_versions:
            if version in experiment['versions']:
                result = experiment['versions'][version]
                if result['coverage'] is not None:
                    row += f" | {result['coverage']:>13.1f}%"
                else:
                    row += f" | {'FAILED':>15}"
            else:
                row += f" | {'N/A':>15}"
        print(row)

    # Find best performing models per version
    print(f"\n🏆 Best Performing Models by Version:")
    for version in all_versions:
        version_name = version_names.get(version, version)
        best_coverage = -1
        best_models = []

        for model_name, experiment in coverage_results.items():
            if version in experiment['versions']:
                result = experiment['versions'][version]
                if result['coverage'] is not None:
                    if result['coverage'] > best_coverage:
                        best_coverage = result['coverage']
                        best_models = [model_name]
                    elif result['coverage'] == best_coverage:
                        best_models.append(model_name)

        if best_models and best_coverage >= 0:
            models_str = ', '.join(best_models)
            print(f"   {version_name:20}: {models_str} ({best_coverage:.1f}%)")
        else:
            print(f"   {version_name:20}: No successful results")

    # Print average coverage per model
    print(f"\n📈 Average Coverage per Model:")
    for model_name, experiment in coverage_results.items():
        coverages = []
        for result in experiment['versions'].values():
            if result['coverage'] is not None:
                coverages.append(result['coverage'])

        if coverages:
            avg_coverage = sum(coverages) / len(coverages)
            print(f"   {model_name:20}: {avg_coverage:6.1f}% (from {len(coverages)} versions)")
        else:
            print(f"   {model_name:20}: No successful results")

def parse_agent_metrics(experiment_folder):
    """Parse agent metrics from comprehensive_results.txt file.

    Args:
        experiment_folder (str): Path to experiment folder

    Returns:
        dict: Parsed metrics or None if parsing fails
    """
    try:
        sva_dir = os.path.join(experiment_folder, 'sva')
        results_file = os.path.join(sva_dir, 'comprehensive_results.txt')

        if not os.path.exists(results_file):
            print(f"⚠️  Results file not found: {results_file}")
            return None

        metrics = {}

        with open(results_file, 'r') as f:
            content = f.read()

        # Parse assertion metrics
        metrics['initial_assertions'] = extract_metric(content, r'Initial assertions:\s+(\d+)')
        metrics['after_syntax_assertions'] = extract_metric(content, r'After syntax correction:\s+(\d+)')
        metrics['after_expansion_assertions'] = extract_metric(content, r'After property set expansion:\s+(\d+)')
        metrics['final_assertions'] = extract_metric(content, r'Final assertions:\s+(\d+)')

        # Calculate derived metrics
        if metrics['initial_assertions'] is not None and metrics['final_assertions'] is not None:
            metrics['total_added'] = metrics['final_assertions'] - metrics['initial_assertions']

        # Parse verification results (After Semantic Correction for golden, Before for validation)
        metrics['passing_after_semantic'] = extract_metric(content, r'Passing assertions:\s+(\d+)')
        metrics['failing_after_semantic'] = extract_metric(content, r'Failing assertions:\s+(\d+)')

        # Calculate success rate
        if metrics['final_assertions'] is not None and metrics['failing_after_semantic'] is not None:
            proven_assertions = metrics['final_assertions'] - metrics['failing_after_semantic']
            metrics['success_rate'] = (proven_assertions / metrics['final_assertions'] * 100) if metrics['final_assertions'] > 0 else 0

        # Parse timing metrics (in seconds) - exact format from _save_timing_results
        metrics['syntax_time'] = extract_metric(content, r'Syntax correction:\s+([\d.]+)s')
        metrics['extension_time'] = extract_metric(content, r'Property set extension:\s+([\d.]+)s')
        metrics['semantic_time'] = extract_metric(content, r'Semantic correction:\s+([\d.]+)s')

        # Parse API usage - exact format from _save_timing_results
        metrics['total_tokens'] = extract_metric(content, r'Total tokens:\s+([\d,]+)')
        metrics['total_api_calls'] = extract_metric(content, r'Total API calls:\s+(\d+)')

        # Clean up comma-separated numbers
        if metrics['total_tokens'] is not None:
            metrics['total_tokens'] = int(str(metrics['total_tokens']).replace(',', ''))

        # Calculate avg tokens per call
        if metrics['total_tokens'] is not None and metrics['total_api_calls'] is not None and metrics['total_api_calls'] > 0:
            metrics['avg_tokens_per_call'] = metrics['total_tokens'] // metrics['total_api_calls']

        return metrics

    except Exception as e:
        print(f"❌ Error parsing metrics from {experiment_folder}: {str(e)}")
        return None

def extract_metric(content, pattern):
    """Extract a metric value from content using regex pattern.

    Args:
        content (str): File content to search
        pattern (str): Regex pattern to match

    Returns:
        int/float/None: Extracted value or None if not found
    """
    import re
    match = re.search(pattern, content)
    if match:
        value_str = match.group(1).replace(',', '')
        try:
            # Try int first, then float
            if '.' in value_str:
                return float(value_str)
            else:
                return int(value_str)
        except ValueError:
            return None
    return None

def print_detailed_model_comparison(results):
    """Print detailed comparison tables between models.

    Args:
        results (list): List of experiment results
    """
    if not results:
        return

    print(f"\n📊 Parsing experiment metrics for detailed comparison...")

    # Filter successful experiments and parse their metrics
    successful_results = []
    for result in results:
        if result['success'] and result['folder']:
            model_short = sanitize_model_name(result['model'])
            print(f"   📂 Analyzing {model_short}: {result['folder']}")
            metrics = parse_agent_metrics(result['folder'])
            if metrics:
                successful_results.append({
                    'model': result['model'],
                    'duration': result['duration'],
                    'folder': result['folder'],
                    'metrics': metrics
                })
                print(f"   ✅ Successfully parsed metrics for {model_short}")
            else:
                print(f"   ❌ Failed to parse metrics for {model_short}")

    if not successful_results:
        print("⚠️  No metrics data available for detailed comparison")
        print("   💡 Make sure experiments completed successfully and generated comprehensive_results.txt files")
        return

    print(f"\n🔍 DETAILED MODEL COMPARISON")
    print(f"{'='*80}")

    # Table 1: Assertion Generation Statistics
    print(f"\n📊 Assertion Generation Statistics:")
    print(f"{'Model':<18} | {'Initial':>7} | {'Final':>5} | {'Added':>5} | {'Removed':>7} | {'Success Rate':>12}")
    print(f"{'-'*68}")

    for result in successful_results:
        model_short = sanitize_model_name(result['model'])
        metrics = result['metrics']

        initial = metrics.get('initial_assertions', 0)
        final = metrics.get('final_assertions', 0)
        added = metrics.get('total_added', 0)
        removed = max(0, initial + added - final) if initial and final and added is not None else 0
        success_rate = metrics.get('success_rate', 0)

        print(f"{model_short:<18} | {initial:>7} | {final:>5} | {added:>5} | {removed:>7} | {success_rate:>10.1f}%")

    # Table 2: Execution Time Breakdown
    print(f"\n⏱️  Execution Time Breakdown:")
    print(f"{'Model':<18} | {'Syntax':>8} | {'Extension':>11} | {'Semantic':>9} | {'Total':>9}")
    print(f"{'-'*60}")

    for result in successful_results:
        model_short = sanitize_model_name(result['model'])
        metrics = result['metrics']

        syntax_time = metrics.get('syntax_time', 0) / 60.0
        extension_time = metrics.get('extension_time', 0) / 60.0
        semantic_time = metrics.get('semantic_time', 0) / 60.0
        total_time = result['duration'] / 60.0

        print(f"{model_short:<18} | {syntax_time:>6.1f}m | {extension_time:>9.1f}m | {semantic_time:>7.1f}m | {total_time:>7.1f}m")

    # Table 3: LLM API Usage Comparison
    print(f"\n🔢 LLM API Usage Comparison:")
    print(f"{'Model':<18} | {'Total Tokens':>13} | {'API Calls':>9} | {'Avg Tokens/Call':>15}")
    print(f"{'-'*61}")

    for result in successful_results:
        model_short = sanitize_model_name(result['model'])
        metrics = result['metrics']

        total_tokens = metrics.get('total_tokens', 0)
        api_calls = metrics.get('total_api_calls', 0)
        avg_tokens = metrics.get('avg_tokens_per_call', 0)

        print(f"{model_short:<18} | {total_tokens:>11,} | {api_calls:>9} | {avg_tokens:>13,}")

def main():
    """Main function for multi-LLM experiment runner."""

    print("🧪 AI-SVA Multi-LLM Experiment Runner")
    print("   Executes the same RTL module with multiple LLM models")
    print("   Preserves results by renaming testbench folders")
    print("="*80)

    # Parse arguments
    args = parse_arguments()

    # Prompt for missing required arguments
    args = prompt_for_missing_args(args)

    # Find the RTL module file (check current dir first, then source dirs)
    # This mirrors the logic from main.py to ensure consistent path resolution
    rtl_module_path = find_rtl_module_file(args.rtl_module, args.sources, args.includes)

    # Verify RTL module exists
    if not os.path.exists(rtl_module_path):
        print(f"❌ RTL module not found: {args.rtl_module}")
        print(f"   Searched in directories: {args.sources + [args.includes] if args.includes else args.sources}")
        sys.exit(1)

    # Update the module path for all subsequent operations (CRITICAL FIX)
    args.rtl_module = rtl_module_path
    print(f"📄 Using resolved RTL module path: {args.rtl_module}")

    # Check for required scripts if coverage analysis is requested
    if args.run_coverage:
        required_scripts = [
            os.path.join(UTILITY_SCRIPTS_DIR, 'copy_prop.sh'),
            os.path.join(FPV_SCRIPTS_DIR, 'run_jg_batch.sh'),
        ]
        missing_scripts = []

        for script in required_scripts:
            if not os.path.exists(script):
                missing_scripts.append(script)
            elif not os.access(script, os.X_OK):
                print(f"⚠️  Making {script} executable...")
                os.chmod(script, 0o755)

        if missing_scripts:
            print(f"❌ Required scripts not found for coverage analysis: {missing_scripts}")
            print(f"   Please ensure these scripts are available in the current directory.")
            if not input("Continue without coverage analysis? (y/n): ").strip().lower().startswith('y'):
                sys.exit(1)
            args.run_coverage = False

    # Generate experiment name if not provided
    if not args.experiment_name:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        args.experiment_name = timestamp

    # Display configuration
    print(f"\n📋 Experiment Configuration:")
    print(f"   RTL Module: {args.rtl_module}")
    print(f"   Module Type: {args.module_type}")
    print(f"   Execution Type: {args.execution_type}")
    print(f"   Verbosity: {args.verbosity}")
    print(f"   Sources: {args.sources}")
    print(f"   Includes: {args.includes}")
    print(f"   Assertion Source: {args.assertion_source}")
    if args.assertion_source == 'file':
        print(f"   Initial File: {args.initial_file}")
    print(f"   Package Auto-detection: {'DISABLED' if args.disable_package_detection else 'ENABLED'}")
    print(f"   Experiment Name: {args.experiment_name}")
    print(f"   Keep Intermediate Files: {args.keep_intermediate}")
    print(f"   Run Coverage Analysis: {args.run_coverage}")
    if args.run_coverage:
        version_names = {'i': 'Initial', 's': 'After Syntax', 'e': 'After Extension', 'c': 'After Semantic Correction', 'f': 'Final'}
        versions_str = ', '.join([version_names.get(v, v) for v in args.coverage_versions])
        print(f"   Coverage Versions: {versions_str}")

    print(f"\n🤖 LLM Models for experiments:")
    for i, model in enumerate(args.llm_models, 1):
        print(f"   {i}. {model}")

    '''
    # Confirm before starting
    try:
        response = input(f"\n🚀 Ready to run {len(args.llm_models)} experiments. Continue? (y/n): ").strip().lower()
        if response not in ['y', 'yes']:
            print("🛑 Experiments cancelled by user.")
            sys.exit(0)
    except KeyboardInterrupt:
        print("\n🛑 Experiments cancelled by user.")
        sys.exit(0)
    '''

    # Run experiments
    total_start_time = time.time()
    results = []
    successful_experiments = 0

    for i, llm_model in enumerate(args.llm_models, 1):
        print(f"\n🔄 Preparing experiment {i}/{len(args.llm_models)}...")

        # Run the experiment
        success, duration = run_experiment(args, llm_model, i, len(args.llm_models))

        # Record results
        results.append({
            'model': llm_model,
            'success': success,
            'duration': duration,
            'folder': None,
            'all_folders': []
        })

        if success:
            successful_experiments += 1

            # Rename all testbench folders (main + submodules)
            renamed_folders = rename_testbench_folders(args.rtl_module, llm_model, args.experiment_name)
            main_folder = None

            # Find the main module folder from the renamed folders
            module_name = os.path.splitext(os.path.basename(args.rtl_module))[0]
            main_folder_prefix = f'ft_{module_name}_'
            for folder in renamed_folders:
                if folder.startswith(main_folder_prefix):
                    main_folder = folder
                    break

            # Store both main folder and all renamed folders
            results[-1]['folder'] = main_folder or (renamed_folders[0] if renamed_folders else None)
            results[-1]['all_folders'] = renamed_folders

            # Clean up intermediate files
            cleanup_intermediate_files(args.keep_intermediate)

            print(f"✅ Experiment {i} completed and results preserved!")
        else:
            print(f"❌ Experiment {i} failed!")
            '''
            # Ask if user wants to continue
            if i < len(args.llm_models):
                try:
                    response = input(f"\n⚠️  Experiment {i} failed. Continue with remaining experiments? (y/n): ").strip().lower()
                    if response not in ['y', 'yes']:
                        print("🛑 Remaining experiments cancelled by user.")
                        break
                except KeyboardInterrupt:
                    print("\n🛑 Remaining experiments cancelled by user.")
                    break
            '''

    total_end_time = time.time()
    total_duration = total_end_time - total_start_time

    # Final summary
    print(f"\n{'='*80}")
    print(f"🎉 MULTI-LLM EXPERIMENT SUMMARY")
    print(f"{'='*80}")

    print(f"⏱️  Total execution time: {total_duration:.2f} seconds ({total_duration/60:.1f} minutes)")
    print(f"📊 Experiments completed: {len(results)}/{len(args.llm_models)}")
    print(f"✅ Successful: {successful_experiments}")
    print(f"❌ Failed: {len(results) - successful_experiments}")

    print(f"\n📋 Detailed Results:")
    for i, result in enumerate(results, 1):
        status = "✅ SUCCESS" if result['success'] else "❌ FAILED"
        duration_str = f"{result['duration']:.1f}s"
        model_short = sanitize_model_name(result['model'])

        print(f"   {i}. {model_short:20} | {status:10} | {duration_str:8} | {result['folder'] or 'N/A'}")

    if successful_experiments > 0:
        print(f"\n📁 Generated testbench folders:")
        for i, result in enumerate(results, 1):
            if result['success'] and result.get('all_folders'):
                model_short = sanitize_model_name(result['model'])
                all_folders = result.get('all_folders', [])
                main_folder = result.get('folder')

                print(f"   {i}. {model_short}:")
                print(f"      Main: {main_folder}")

                # Show submodule folders if any
                submodule_folders = [f for f in all_folders if f != main_folder]
                if submodule_folders:
                    print(f"      Submodules ({len(submodule_folders)}):")
                    for sub_folder in submodule_folders:
                        print(f"        • {sub_folder}")
                else:
                    print(f"      (No submodules detected)")
            elif result['success'] and result['folder']:
                # Fallback for legacy format
                model_short = sanitize_model_name(result['model'])
                print(f"   {i}. {model_short}: {result['folder']}")

    # Print detailed model comparison tables
    if successful_experiments > 0:
        print_detailed_model_comparison(results)

    # Run post-experiment coverage analysis
    coverage_results = {}
    if successful_experiments > 0 and args.run_coverage:
        try:
            coverage_results = run_post_experiment_coverage_analysis(results, args.rtl_module, args.coverage_versions)
            if coverage_results:
                print_coverage_analysis_summary(coverage_results)
        except Exception as e:
            print(f"⚠️  Coverage analysis encountered an error: {str(e)}")
            print(f"   Continuing with experiment summary...")

    print(f"\n💡 Next steps:")
    if successful_experiments > 0:
        print(f"   • Compare results across different LLM models")
        print(f"   • Analyze assertion quality and coverage differences")
        if coverage_results:
            print(f"   • Review coverage analysis results above")
            print(f"   • Compare property evolution across different stages")
        else:
            print(f"   • Run verification flows on each generated testbench")
    else:
        print(f"   • Check error logs to identify issues")
        print(f"   • Verify RTL module and dependencies are accessible")

    # Exit with appropriate code
    if successful_experiments == 0:
        print(f"\n❌ All experiments failed!")
        sys.exit(1)
    elif successful_experiments < len(args.llm_models):
        print(f"\n⚠️  Some experiments failed, but {successful_experiments} succeeded.")
        sys.exit(1)
    else:
        print(f"\n🎉 All experiments completed successfully!")
        sys.exit(0)

if __name__ == '__main__':
    main()