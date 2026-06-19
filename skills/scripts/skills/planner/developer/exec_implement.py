#!/usr/bin/env python3
"""Exec Implement - Router script that dispatches to execute or fix.

This is a THIN router. The routing logic lives in shared/routing.py.
This file specifies only: which phase key to use.

Dispatches to:
- exec_implement_execute.py: First-time implementation (4 steps)
- quality_reviewer/exec_qr_fix.py (--phase impl-code): Post-QR fix workflow (3 steps)

Selection based on QR state detection:
- No qr-impl-code.json or no FAIL items -> execute
- FAIL items present -> qr_fix
"""

from skills.planner.shared.resources import STATE_DIR_ARG_REQUIRED
from skills.planner.shared.routing import build_route_dispatch

PHASE_KEY = "impl-code"


def get_step_guidance(step: int, module_path: str | None = None, **kwargs) -> dict:
    """Router: dispatch to execute or fix based on state.

    Routing logic -- including the missing-state_dir fail-closed policy -- lives in
    shared/routing.py (ONE place). This file specifies only: which phase key to use.
    """
    if step != 1:
        return {
            "error": "Router only handles step 1. Subsequent steps handled by dispatched script."
        }

    return build_route_dispatch(kwargs.get("state_dir"), PHASE_KEY, "Exec Implement")


if __name__ == "__main__":
    from skills.lib.workflow.cli import mode_main

    mode_main(
        __file__,
        get_step_guidance,
        "Exec-Implement: Router for developer implementation workflows",
        extra_args=[
            STATE_DIR_ARG_REQUIRED,
        ],
    )
