"""Centralized routing logic for work phases.

Definition locality: router pattern logic lives in ONE place.
3 router scripts become thin wrappers that call route_work_phase().

This works by:
1. WORK_PHASES registry defines all work phase routing
2. detect_qr_state() checks for QR failures
3. route_work_phase() returns dispatch target
4. Router scripts call this function instead of duplicating logic

Invariants:
- Each work phase has exactly one entry in WORK_PHASES
- execute/fix scripts are valid Python module paths
- has_failures=True -> dispatch to qr_fix script
- has_failures=False -> dispatch to execute script
"""

from __future__ import annotations

from collections.abc import Callable

from .builders import shell_quote
from .qr.utils import (
    by_blocking_severity,
    by_status,
    get_qr_iteration_from_state,
    load_qr_state,
    query_items,
)

# Work phase routing registry - ALL work phases in ONE place
#
# Keys: phase key used by router scripts
# Values: dict with:
#   execute: Python module for first-time execution
#   qr_fix: Python module for post-QR fix workflow
#   qr_phase: QR phase name for state detection

# All three qr_fix targets are the one shared runner; --phase (== qr_phase) selects the
# per-phase fix content. The execute scripts stay per-phase (first-time creation differs
# structurally). See quality_reviewer/exec_qr_fix.py + prompts/fix.py.
WORK_PHASES: dict[str, dict] = {
    "plan-design": {
        "execute": "skills.planner.architect.plan_design_execute",
        "qr_fix": "skills.planner.quality_reviewer.exec_qr_fix",
        "qr_phase": "plan-design",
    },
    "impl-code": {
        "execute": "skills.planner.developer.exec_implement_execute",
        "qr_fix": "skills.planner.quality_reviewer.exec_qr_fix",
        "qr_phase": "impl-code",
    },
    "impl-docs": {
        "execute": "skills.planner.technical_writer.exec_docs_execute",
        "qr_fix": "skills.planner.quality_reviewer.exec_qr_fix",
        "qr_phase": "impl-docs",
    },
}


def detect_qr_state(state_dir: str, phase: str) -> tuple[bool, list[dict], int]:
    """Detect QR state for routing decision.

    Severity-aware: only FAIL items at blocking severity for the current
    iteration count as failures. A phase with only below-threshold FAIL
    items routes to execute (not fix).

    Args:
        state_dir: Path to state directory
        phase: QR phase name (e.g., "plan-design")

    Returns:
        (has_failures, failed_items, iteration) where:
        - has_failures: True if blocking FAIL items exist
        - failed_items: List of blocking failed item dicts (empty if none)
        - iteration: current QR iteration (1 when no state file); returned so the
          caller reuses this single load instead of re-reading qr-{phase}.json
    """
    qr_state = load_qr_state(state_dir, phase)
    if not qr_state:
        return (False, [], 1)
    iteration = get_qr_iteration_from_state(qr_state)
    blocking_failures = query_items(qr_state, by_status("FAIL"), by_blocking_severity(iteration))
    return (len(blocking_failures) > 0, blocking_failures, iteration)


def route_work_phase(state_dir: str, phase_key: str) -> dict:
    """Determine dispatch target and build dispatch output.

    Centralized routing logic - router scripts call this function
    instead of duplicating the detect/route pattern.

    This works by:
    1. Look up phase config from WORK_PHASES registry
    2. detect_qr_state() checks for qr-{phase}.json and FAIL items
    3. has_failures=True -> dispatch to qr_fix script
    4. has_failures=False -> dispatch to execute script

    Args:
        state_dir: Path to state directory
        phase_key: Work phase key (e.g., "plan-design", "impl-code")

    Returns:
        Dict with:
        - target_module: Python module path to dispatch to
        - has_failures: Whether QR failures exist
        - failed_count: Number of failed items (0 if none)
        - iteration: current QR iteration (from the single detect_qr_state load)

    Raises:
        ValueError: If phase_key is unknown
    """
    if phase_key not in WORK_PHASES:
        valid = ", ".join(sorted(WORK_PHASES.keys()))
        raise ValueError(f"Unknown work phase: {phase_key}. Valid phases: {valid}")

    config = WORK_PHASES[phase_key]
    has_failures, failed_items, iteration = detect_qr_state(state_dir, config["qr_phase"])

    target = config["qr_fix"] if has_failures else config["execute"]

    return {
        "target_module": target,
        "has_failures": has_failures,
        "failed_count": len(failed_items),
        "iteration": iteration,
    }


def build_route_dispatch(
    state_dir: str | None,
    phase_key: str,
    title_stem: str,
    execute_actions_provider: Callable[[], list[str] | None] | None = None,
) -> dict:
    """Build the work-phase router's fix/execute dispatch dict.

    Single owner of the {title, actions, dispatch_to, next} shape the three thin
    routers emit, AND of the missing-state_dir fail-closed policy. Calls
    route_work_phase once, shell-quotes state_dir in the next command, and reuses the
    iteration route_work_phase already loaded for the fix-mode message (no second read
    of qr-{phase}.json). execute_actions_provider, invoked only in the execute branch,
    lets a router override the default execute message (the architect surfaces
    completeness gaps); returning None falls back to the default.
    """
    # Single owner of the missing-state_dir policy for all three thin routers: a
    # router invoked without --state-dir cannot detect QR state, so fail closed here
    # (rather than each router hand-rolling its own guard and drifting on the answer).
    if not state_dir:
        return {"error": "--state-dir required"}

    result = route_work_phase(state_dir, phase_key)
    target = result["target_module"]

    if result["has_failures"]:
        iteration = result["iteration"]
        # The fix target is the shared phase-parameterized runner (exec_qr_fix), so the
        # dispatched command must carry --phase (== qr_phase == phase_key) to select the
        # right fix content; the execute scripts below take no --phase.
        fix_cmd = (
            f"uv run python -m {target} --step 1 "
            f"--phase {phase_key} --state-dir {shell_quote(state_dir)}"
        )
        return {
            "title": f"{title_stem} - Routing to Fix Mode",
            "actions": [
                f"QR state detected: {result['failed_count']} failed items (iteration {iteration})",
                "Dispatching to FIX workflow.",
            ],
            "dispatch_to": target,
            "next": fix_cmd,
        }

    next_cmd = f"uv run python -m {target} --step 1 --state-dir {shell_quote(state_dir)}"
    actions = execute_actions_provider() if execute_actions_provider else None
    if actions is None:
        actions = [
            "First-time execution or no QR failures.",
            "Dispatching to EXECUTE workflow.",
        ]
    return {
        "title": f"{title_stem} - Routing to Execute Mode",
        "actions": actions,
        "dispatch_to": target,
        "next": next_cmd,
    }
