#!/usr/bin/env python3
"""Exec Docs - Router script that dispatches to execute or fix.

This is a THIN router. The routing logic lives in shared/routing.py.
This file specifies only: which phase key to use.

Dispatches to:
- exec_docs_execute.py: First-time documentation (6 steps)
- exec_docs_qr_fix.py: Post-QR fix workflow (3 steps)

Selection based on QR state detection:
- No qr-impl-docs.json or no FAIL items -> execute
- FAIL items present -> qr_fix
"""

from skills.planner.shared.routing import build_route_dispatch

PHASE_KEY = "impl-docs"


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

    # If no state_dir provided, default to execute mode
    if not state_dir:
        target = "skills.planner.technical_writer.exec_docs_execute"
        return {
            "title": "Exec Docs - Routing to Execute Mode",
            "actions": [
                "No state directory provided.",
                "Dispatching to EXECUTE workflow.",
            ],
            "dispatch_to": target,
            "next": f"uv run python -m {target} --step 1",
        }

    return build_route_dispatch(state_dir, PHASE_KEY, "Exec Docs")


if __name__ == "__main__":
    from skills.lib.workflow.cli import mode_main

    mode_main(
        __file__,
        get_step_guidance,
        "Exec-Docs: Router for technical writer post-impl workflows",
        extra_args=[
            (["--state-dir"], {"type": str, "help": "State directory path"}),
        ],
    )
