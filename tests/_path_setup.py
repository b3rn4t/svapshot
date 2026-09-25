"""Make the repository's src layout importable in direct test execution."""

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / 'src' / 'core'
ANALYSIS_DIR = REPO_ROOT / 'src' / 'analysis'

for path in (REPO_ROOT, CORE_DIR, ANALYSIS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
