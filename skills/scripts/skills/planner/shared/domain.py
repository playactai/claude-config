"""Domain types for the planner skill.

Planner-specific types that extend the shared workflow types. NextCommand is
imported from skills.lib.workflow.types as the type of GuidanceResult.next_command.
"""

from dataclasses import dataclass

from skills.lib.workflow.types import NextCommand

__all__ = [
    "GuidanceResult",
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
