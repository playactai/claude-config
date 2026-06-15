"""Workflow step configuration constants.

WHY: Total step counts were hardcoded in multiple locations:
- STEPS dicts (implicit via key count)
- STEP_HANDLERS range() calls
- Final step fallback logic (magic number 6)

Centralizing these constants prevents drift and makes
workflow structure explicit.
"""

from skills.lib.workflow.types import AgentRole

# Planner orchestrator workflow (6 steps: plan-design phase + QR, plan approved at step 6)
PLANNER_TOTAL_STEPS = 6
PLANNER_GATE_STEPS = frozenset({6})  # QR route step (gate)

# Executor: QR phase for each step (steps 1, 10 have no QR phase)
EXECUTOR_STEP_PHASES: dict[int, str] = {
    2: "impl-code",
    3: "impl-code",
    4: "impl-code",
    5: "impl-code",
    6: "impl-docs",
    7: "impl-docs",
    8: "impl-docs",
    9: "impl-docs",
}

# Executor: human-facing QR name stem per phase. Single source for both the gate
# titles (EXECUTOR_GATE_CONFIG below) and the decompose/verify step titles in
# executor.py, so the "Code QR"/"Doc QR" strings live in exactly one place.
PHASE_QR_NAME: dict[str, str] = {"impl-code": "Code QR", "impl-docs": "Doc QR"}

# Executor: gate step -> (qr_name, work_step, pass_step, pass_message, fix_target)
EXECUTOR_GATE_CONFIG: dict[int, tuple] = {
    5: (
        PHASE_QR_NAME["impl-code"],
        2,
        6,
        "Code quality verified. Proceed to documentation.",
        AgentRole.DEVELOPER,
    ),
    9: (
        PHASE_QR_NAME["impl-docs"],
        6,
        10,
        "Documentation verified. Proceed to retrospective.",
        AgentRole.TECHNICAL_WRITER,
    ),
}
