"""Verdicts and exit codes for `ts-review`.

`cli.py` imports from here. Later modules (`compose.py`) import from `verdict.py`,
never from `cli.py` — this module has no dependents above it in the pipeline.
"""

from __future__ import annotations

from enum import Enum


class Verdict(str, Enum):
    APPROVE = 'APPROVE'
    CHANGES_REQUESTED = 'CHANGES_REQUESTED'
    NEEDS_HUMAN = 'NEEDS_HUMAN'


EXIT_APPROVE = 0
EXIT_TOOL_FAILURE = 1
EXIT_CHANGES_REQUESTED = 2
EXIT_NEEDS_HUMAN = 3
