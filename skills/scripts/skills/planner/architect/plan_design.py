#!/usr/bin/env python3
"""Plan Design - Router script that dispatches to execute or fix.

This is a THIN router. The routing logic lives in shared/routing.py.
This file specifies only: which phase key to use.

Dispatches to:
- plan_design_execute.py: First-time plan creation (6 steps)
- quality_reviewer/exec_qr_fix.py (--phase plan-design): Post-QR fix workflow (3 steps)

Selection based on QR state detection:
- No qr-plan-design.json or no FAIL items -> execute
- FAIL items present -> qr_fix
"""

from skills.planner.shared.resources import STATE_DIR_ARG_REQUIRED
from skills.planner.shared.routing import build_route_dispatch

PHASE_KEY = "plan-design"


def _completeness_gaps(state_dir: str) -> list[str]:
    """Structural plan-design gaps to surface to a re-dispatched architect.

    Delegates to the single shared helper so the architect router, the QR gate,
    and the executor all read the completeness contract from one place and
    cannot drift. The router alone passes suppress_if_no_milestones=True: at
    step 1 an empty skeleton is first-time execution, not a repairable gap (the
    gate and executor keep the default and fail closed on a milestone-less plan).
    """
    from skills.planner.shared.schema import plan_completeness_errors

    return plan_completeness_errors(state_dir, PHASE_KEY, suppress_if_no_milestones=True)


def _execute_gap_actions(state_dir: str) -> list[str] | None:
    """Execute-mode router actions for the architect.

    Returns None when the plan has no structural gaps (the router renders its
    default execute message), else the gap-repair block surfaced to the architect.
    """
    gaps = _completeness_gaps(state_dir)
    if not gaps:
        return None
    return [
        "Plan approval is blocked by structural gaps (enforced at the step-6 gate):",
        *(f"  - {g}" for g in gaps),
        "",
        "Repair these in the EXECUTE workflow -- in particular author the missing",
        "execution waves (set-wave) so every code milestone is covered by exactly",
        "one wave and no documentation-only milestone sits in a wave.",
    ]


def get_step_guidance(step: int, module_path: str | None = None, **kwargs) -> dict:
    """Router: dispatch to execute or fix based on state.

    Routing logic lives in shared/routing.py (ONE place).
    This file specifies only: which phase key to use.
    """
    if step != 1:
        return {
            "error": "Router only handles step 1. Subsequent steps handled by dispatched script."
        }

    state_dir = kwargs.get("state_dir")
    if not state_dir:
        return {"error": "--state-dir required"}

    return build_route_dispatch(
        state_dir,
        PHASE_KEY,
        "Plan Design",
        execute_actions_provider=lambda: _execute_gap_actions(state_dir),
    )


if __name__ == "__main__":
    from skills.lib.workflow.cli import mode_main

    mode_main(
        __file__,
        get_step_guidance,
        "Plan-Design: Router for architect workflows",
        extra_args=[
            STATE_DIR_ARG_REQUIRED,
        ],
    )
