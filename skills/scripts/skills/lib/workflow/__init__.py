"""Workflow orchestration framework for skills.

Public API for workflow types, formatters, registration, and testing.
"""

from .core import Arg, StepDef, Workflow
from .discovery import discover_workflows
from .types import (
    PHASE_TO_MODE,
    AgentRole,
    BranchRouting,
    Confidence,
    Dispatch,
    LinearRouting,
    Mode,
    Phase,
    Routing,
    TerminalRouting,
)

__all__ = [
    "PHASE_TO_MODE",
    # Domain types
    "AgentRole",
    "Arg",
    "BranchRouting",
    "Confidence",
    "Dispatch",
    "LinearRouting",
    "Mode",
    # Code quality document types
    "Phase",
    "Routing",
    "StepDef",
    "TerminalRouting",
    # Core types
    "Workflow",
    "discover_workflows",
]
