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

from skills.planner.shared.qr.utils import get_qr_iteration, has_qr_failures
from skills.planner.shared.resources import STATE_DIR_ARG_REQUIRED
from skills.planner.shared.routing import route_work_phase

PHASE_KEY = "plan-design"


def _completeness_gaps(state_dir: str) -> list[str]:
    """Structural plan-design gaps to surface to a re-dispatched architect.

    The step-6 gate vetoes an otherwise QR-passing plan that fails
    validate_completeness (e.g. a code milestone in no wave) and routes back here.
    QR-pass means no FAIL items, so this lands in EXECUTE mode with no findings to
    act on -- listing the deterministic gaps gives the architect targeted guidance,
    the convergence pressure the QR loop gets from its FAIL findings.

    Gated on milestones existing: an empty skeleton is genuine first-time
    execution, not a repairable gap, so it stays silent there.
    """
    import json
    from pathlib import Path

    from skills.planner.shared.schema import Plan

    path = Path(state_dir) / "plan.json"
    if not path.exists():
        return []
    try:
        plan = Plan.model_validate(json.loads(path.read_text()))
    except Exception:
        return []
    if not plan.milestones:
        return []
    return plan.validate_completeness(PHASE_KEY)


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

    # Check fix mode via file state inspection
    if has_qr_failures(state_dir, PHASE_KEY):
        iteration = get_qr_iteration(state_dir, PHASE_KEY)
        target = "skills.planner.architect.plan_design_qr_fix"
        return {
            "title": "Plan Design - Routing to Fix Mode",
            "actions": [
                f"QR failures detected (iteration {iteration})",
                "Dispatching to FIX workflow.",
            ],
            "dispatch_to": target,
            "next": f"uv run python -m {target} --step 1 --state-dir {state_dir}",
        }

    # Use routing module for state-based detection
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
