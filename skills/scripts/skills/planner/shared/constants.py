"""Workflow step configuration constants.

WHY: Total step counts were hardcoded in multiple locations:
- STEPS dicts (implicit via key count)
- STEP_HANDLERS range() calls
- Final step fallback logic (magic number 6)

Centralizing these constants prevents drift and makes
workflow structure explicit.
"""

# Planner orchestrator workflow (6 steps: plan-design phase + QR, plan approved at step 6)
PLANNER_TOTAL_STEPS = 6
PLANNER_GATE_STEPS = frozenset({6})  # QR route step (gate)

# Executor orchestrator workflow (10 steps with parallel QR)
EXECUTOR_TOTAL_STEPS = 10
EXECUTOR_GATE_STEPS = frozenset({5, 9})

# Sub-workflow step counts
PLAN_DESIGN_TOTAL_STEPS = 6
EXEC_IMPLEMENT_TOTAL_STEPS = 4
EXEC_DOCS_TOTAL_STEPS = 6
