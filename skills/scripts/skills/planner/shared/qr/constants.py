"""QR workflow constants.

Moved from lib/workflow/constants.py to planner/shared/qr/constants.py.
"""

QR_ITERATION_LIMIT = 5
QR_ITERATION_DEFAULT = 1

# Verify-dispatch parallelism tuning (audit §2 leak 2). The orchestrator re-bins
# the decompose agent's affinity groups into at most VERIFY_MAX_PARALLEL balanced
# agents of ~VERIFY_TARGET_PER_GROUP items each, so one fat group can't serialize
# the phase and N singletons can't each pay the fixed per-agent context-load cost.
VERIFY_MAX_PARALLEL = 8
VERIFY_TARGET_PER_GROUP = 3

# CLI argument defaults - single source of truth
CLI_DEFAULTS = {
    "qr_iteration": 1,
    "qr_status": None,
    "mode": None,
    "state_dir": None,
}


def get_cli_default(arg_name: str):
    """Get default value for CLI argument.

    WHY: kwargs.get("qr_iteration", 1) was repeated in 10+ files.
    Using this function ensures all defaults stay synchronized.
    """
    return CLI_DEFAULTS.get(arg_name)


def get_qa_state_file(phase: str) -> str:
    """Get QA state file for a specific phase."""
    return f"qr-{phase}.json"


def get_blocking_severities(iteration: int) -> frozenset[str]:
    """Return severities that block at given iteration.

    Progressive de-escalation narrows blocking scope as iterations
    increase, accepting lower-severity issues rather than looping
    indefinitely:
        iteration 1-2: MUST + SHOULD + COULD
        iteration 3:   MUST + SHOULD
        iteration 4+:  MUST only

    Threshold rationale per conventions/severity.md:
    - Iterations 1-2 give full coverage (all severities verified).
    - Iteration 3 drops COULD (cosmetic/auto-fixable). Two fix
      attempts is sufficient for low-impact items.
    - Iteration 4 drops SHOULD (structural debt). Only MUST
      (knowledge loss risks) justifies blocking a plan indefinitely.

    Args:
        iteration: QR loop iteration count (1-indexed)

    Returns:
        Frozenset of severity strings that block at this iteration
    """
    if iteration >= 4:
        return frozenset({"MUST"})
    if iteration >= 3:
        return frozenset({"MUST", "SHOULD"})
    return frozenset({"MUST", "SHOULD", "COULD"})


def get_iteration_guidance_message(iteration: int) -> str:
    """Get user-facing message about current iteration state."""
    blocking = get_blocking_severities(iteration)
    # Severity priority order, not alphabetical
    severity_order = ["MUST", "SHOULD", "COULD"]
    levels = ", ".join(s for s in severity_order if s in blocking)
    return f"Iteration {iteration}: blocking on {levels}."
