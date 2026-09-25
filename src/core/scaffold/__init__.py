"""Native formal-harness scaffolding for SVApshot.

Call ``scaffold_harness`` (or ``main.run_scaffold``, which wraps it) to build
the ``ft_<module>/`` tree that the rest of the flow consumes.
"""

from scaffold.assertions import (
    assemble_assertion,
    escape_display_string,
    normalize_assertion_id,
)
from scaffold.engine import (
    ScaffoldError,
    main_cli,
    reset_scaffold_state,
    scaffold_harness,
)

__all__ = [
    'ScaffoldError',
    'assemble_assertion',
    'escape_display_string',
    'main_cli',
    'normalize_assertion_id',
    'reset_scaffold_state',
    'scaffold_harness',
]
