"""QR workflow constants.

Moved from lib/workflow/constants.py to planner/shared/qr/constants.py.
"""

QR_ITERATION_LIMIT = 5

# Verify-dispatch parallelism tuning (audit §2 leak 2). The orchestrator re-bins
# the decompose agent's affinity groups into at most VERIFY_MAX_PARALLEL balanced
# agents of ~VERIFY_TARGET_PER_GROUP items each, so one fat group can't serialize
# the phase and N singletons can't each pay the fixed per-agent context-load cost.
VERIFY_MAX_PARALLEL = 8
VERIFY_TARGET_PER_GROUP = 3


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
