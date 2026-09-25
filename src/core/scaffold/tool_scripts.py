"""Constants shared by JasperGold / SymbiYosys harness file-list writers.

The concrete ``FPV.tcl`` / ``files.vc`` emitters live in ``engine`` next to the
clock and submodule state they need; this module holds the library-walk policy
so it can be tested without scaffolding a full ``ft_*`` tree.
"""

from __future__ import annotations

#: Subtrees of a ``-src`` walk that must not become ``-y`` library directories.
#: AssertLLM and similar corpora ship mutation and buggy copies of the same
#: module names beside the golden RTL; searching them lets VCS bind the wrong
#: definition.
SKIP_LIB_SUBDIRS = frozenset({
    'mutations',
    'buggy_artifacts',
    'figures',
    '__pycache__',
    '.git',
})


def should_skip_lib_subdir(name: str) -> bool:
    """True when *name* must not be added as a ``-y`` library directory."""
    return name in SKIP_LIB_SUBDIRS or name.startswith('.')
