"""Domain types for the planner skill.

Planner-specific types that extend the shared workflow types.
Command routing types (FlatCommand, BranchCommand, NextCommand) are
re-exported from skills.lib.workflow.types for backwards compatibility.
"""

from dataclasses import dataclass

# Re-export command routing types from lib (backwards compatibility)
from skills.lib.workflow.types import (
    BranchCommand,
    FlatCommand,
    NextCommand,
)

__all__ = [
    "BranchCommand",
    "FlatCommand",
    "GuidanceResult",
    "NextCommand",
]

# =============================================================================
# Step Guidance
# =============================================================================


@dataclass
class GuidanceResult:
    """Step guidance returned by get_*_guidance functions.

    Replaces stringly-typed dicts with explicit structure.

    Attributes:
        title: Step title for display
        actions: List of action strings (may include XML blocks)
        next_command: Routing command for invoke_after
    """

    title: str
    actions: list[str]
    next_command: NextCommand = None
