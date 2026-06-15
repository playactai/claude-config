#!/usr/bin/env python3
"""Plan Design - Router script that dispatches to execute or fix.

This is a THIN router. The routing logic lives in shared/routing.py.
This file specifies only: which phase key to use.

Dispatches to:
- plan_design_execute.py: First-time plan creation (6 steps)
- plan_design_qr_fix.py: Post-QR fix workflow (3 steps)

Selection based on QR state detection:
- No qr-plan-design.json or no FAIL items -> execute
- FAIL items present -> qr_fix
"""

from skills.planner.shared.qr.utils import get_qr_iteration
from skills.planner.shared.resources import STATE_DIR_ARG_REQUIRED
from skills.planner.shared.routing import route_work_phase

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

    # Single detection: route_work_phase reads QR state once and decides fix vs execute.
    result = route_work_phase(state_dir, PHASE_KEY)

    if result["has_failures"]:
        iteration = get_qr_iteration(state_dir, PHASE_KEY)
        return {
            "title": "Plan Design - Routing to Fix Mode",
            "actions": [
                f"QR state detected: {result['failed_count']} failed items (iteration {iteration})",
                "Dispatching to FIX workflow.",
            ],
            "dispatch_to": result["target_module"],
            "next": f"uv run python -m {result['target_module']} --step 1 --state-dir {state_dir}",
        }
    else:
        gaps = _completeness_gaps(state_dir)
        if gaps:
            actions = [
                "Plan approval is blocked by structural gaps (enforced at the step-6 gate):",
                *(f"  - {g}" for g in gaps),
                "",
                "Repair these in the EXECUTE workflow -- in particular author the missing",
                "execution waves (set-wave) so every code milestone is covered by exactly",
                "one wave and no documentation-only milestone sits in a wave.",
            ]
        else:
            actions = [
                "First-time execution or no QR failures.",
                "Dispatching to EXECUTE workflow.",
            ]
        return {
            "title": "Plan Design - Routing to Execute Mode",
            "actions": actions,
            "dispatch_to": result["target_module"],
            "next": f"uv run python -m {result['target_module']} --step 1 --state-dir {state_dir}",
        }


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
