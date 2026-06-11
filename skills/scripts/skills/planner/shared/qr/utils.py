"""QR state utilities for item-level verification and fix workflows.

Consolidated from planner/shared/qr_utils.py.

Provides centralized access to qr-<phase>.json state files:
- load_qr_state: Parse QR state from state directory
- get_qr_item: Single item lookup by ID (for --qr-item verification)
- get_qr_items_by_status: Batch lookup by status (for QR fix workflows)
- format_*: Prompt formatting for different workflows
"""

import contextlib
import fcntl
import json
from collections.abc import Callable, Iterator
from pathlib import Path

from skills.planner.shared.qr.constants import QR_ROUTING
from skills.planner.shared.schema import QA_ITEM_DEFAULTS


@contextlib.contextmanager
def qr_write_lock(state_dir: str | Path, phase: str) -> Iterator[None]:
    """Serialize qr-{phase}.json writers on a stable sentinel inode.

    The data file is replaced via atomic rename on every write, so locking it
    directly provides NO mutual exclusion: a writer that blocks on flock()
    wakes holding a lock on the orphaned pre-rename inode and clobbers the
    writer that won the race (roughly half of concurrent writes are lost
    under load). Locking a sentinel file that is never renamed gives true
    exclusion, while the atomic rename of the data file still gives lock-free
    readers (summary/list/get and the orchestrator's has_qr_failures()) an
    all-or-nothing view.

    Hold this lock across the full read -> mutate -> atomic-write cycle.
    """
    lock_path = Path(state_dir) / f"qr-{phase}.lock"
    with open(lock_path, "a") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        yield
        # Lock released when lock_f closes on context exit.


def load_qr_state(state_dir: str, phase: str) -> dict | None:
    """Load and parse qr-<phase>.json from state directory.

    Args:
        state_dir: Path to state directory
        phase: QR phase name (plan-design, plan-code, plan-docs, impl-code, impl-docs)

    Returns:
        Parsed QR state dict or None if file doesn't exist/is invalid
    """
    path = Path(state_dir) / f"qr-{phase}.json"
    if not path.exists():
        return None

    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def get_qr_item(qr_state: dict, item_id: str) -> dict | None:
    """Get single QR item by ID.

    Args:
        qr_state: Parsed QR state from load_qr_state()
        item_id: Item ID (e.g., "plan-001")

    Returns:
        Item dict or None if not found
    """
    if not qr_state:
        return None

    for item in qr_state.get("items", []):
        if item.get("id") == item_id:
            return item
    return None


def get_qr_items_by_status(qr_state: dict, status: str) -> list[dict]:
    """Get all items with given status.

    Args:
        qr_state: Parsed QR state from load_qr_state()
        status: Status to filter by (TODO, PASS, FAIL)

    Returns:
        List of matching items (empty if none or invalid state)
    """
    if not qr_state:
        return []

    return [item for item in qr_state.get("items", []) if item.get("status") == status]


# Predicate: item dict -> bool. Compose via query_items(*predicates).
ItemPredicate = Callable[[dict], bool]


def by_status(*statuses: str) -> ItemPredicate:
    """Predicate factory: matches items whose status is in statuses.

    Default "TODO" for missing status field because decompose creates
    items without explicit status (TODO is the implicit initial state).
    """
    s = frozenset(statuses)
    return lambda item: item.get("status", "TODO") in s


def by_blocking_severity(iteration: int) -> ItemPredicate:
    """Predicate factory: matches items whose severity blocks at iteration.

    Closes over iteration at construction time. The blocking set is
    resolved once via get_blocking_severities() and captured in the
    closure -- repeated calls to the returned predicate do not
    re-evaluate the threshold.

    Default "SHOULD" for missing severity field because SHOULD is the
    middle tier -- neither blocks indefinitely (MUST) nor is trivially
    skippable (COULD). See shared/schema.py QA_ITEM_DEFAULTS.

    Severity is upper-cased before the membership test so a decompose agent
    that writes lower-case "must"/"should" is not silently downgraded to
    non-blocking. Genuinely out-of-set values stay non-blocking (the tolerant
    runtime contract; schema ingest coerces them to SHOULD for validation).
    """
    from skills.planner.shared.qr.constants import get_blocking_severities

    blocking = get_blocking_severities(iteration)
    return lambda item: str(item.get("severity") or "SHOULD").strip().upper() in blocking


def query_items(qr_state: dict, *predicates: ItemPredicate) -> list[dict]:
    """Filter items by composable predicates applied conjunctively.

    Predicates compose via logical AND: an item is included only if
    all predicates return True. With zero predicates, returns all
    items (identity filter).

    Separation from get_qr_items_by_status: that function is a raw
    data accessor for display/debug. This function applies policy
    filters (status + severity thresholds) for workflow decisions.
    Both coexist: display code calls the raw accessor, routing/gate
    code composes predicates via query_items.

    Args:
        qr_state: Parsed QR state from load_qr_state()
        *predicates: Zero or more item predicates to compose

    Returns:
        List of matching items
    """
    items = qr_state.get("items", []) if qr_state else []
    if not predicates:
        return list(items)
    return [i for i in items if all(p(i) for p in predicates)]


def format_qr_item_for_verification(item: dict) -> str:
    """Format single QR item for verification prompt.

    Used by QR scripts when invoked with --qr-item to verify one item.
    """
    if not item:
        return "ERROR: Item not found"

    lines = [
        "<qr_item_to_verify>",
        f"  <id>{item.get('id', QA_ITEM_DEFAULTS['id'])}</id>",
        f"  <scope>{item.get('scope', QA_ITEM_DEFAULTS['scope'])}</scope>",
        f"  <check>{item.get('check', QA_ITEM_DEFAULTS['check'])}</check>",
        "</qr_item_to_verify>",
        "",
        "VERIFY this specific item. Return exactly:",
        "  PASS - if check passes",
        "  FAIL - if check fails, with finding explaining why",
    ]
    return "\n".join(lines)


def format_failed_items_for_fix(qr_state: dict) -> str:
    """Format all failed items for fixer prompt.

    Used by developer/architect/TW fix scripts when QR failures detected.
    """
    failed = get_qr_items_by_status(qr_state, "FAIL")
    if not failed:
        return ""

    lines = [
        "=" * 60,
        "FAILED QR ITEMS TO FIX (address these FIRST):",
        "=" * 60,
        "",
    ]
    for item in failed:
        lines.append(f"[{item.get('id', '?')}] {item.get('check', '')}")
        if item.get("scope") and item.get("scope") != "*":
            lines.append(f"    Scope: {item['scope']}")
        if item.get("finding"):
            lines.append(f"    Finding: {item['finding']}")
        lines.append("")

    lines.append("=" * 60)
    lines.append("")
    return "\n".join(lines)


def format_todo_items_for_decomposition(qr_state: dict) -> str:
    """Format TODO items remaining to verify.

    Used by QR scripts to show what items still need verification.
    """
    todo = get_qr_items_by_status(qr_state, "TODO")
    if not todo:
        return "All items verified."

    lines = [
        f"REMAINING ITEMS TO VERIFY: {len(todo)}",
        "",
    ]
    for item in todo:
        lines.append(f"  {item.get('id', '?')}: {item.get('check', '')[:60]}...")

    return "\n".join(lines)


def format_qr_result(workflow: str, phase: str, passed: bool, state_dir: str) -> str:
    """Format minimal QR result with invoke_after.

    Args:
        workflow: "planner" or "executor"
        phase: QR phase name (e.g., "plan-design", "impl-code")
        passed: True if all checks passed
        state_dir: Actual state directory path

    Returns:
        Formatted result string with RESULT line and invoke_after command
    """
    key = (workflow, phase)
    if key not in QR_ROUTING:
        raise ValueError(f"Unknown QR routing: workflow={workflow}, phase={phase}")
    gate_step, module_path, _total_steps = QR_ROUTING[key]

    if passed:
        return f"""RESULT: PASS
invoke_after: uv run python -m {module_path} --step {gate_step} --state-dir {state_dir} --qr-status pass"""
    else:
        return f"""RESULT: FAIL
invoke_after: uv run python -m {module_path} --step {gate_step} --state-dir {state_dir} --qr-status fail"""


def get_qr_iteration(state_dir: str, phase: str) -> int:
    """Get current QR iteration from qr-{phase}.json.

    Args:
        state_dir: Path to state directory
        phase: QR phase name (plan-design, plan-code, plan-docs, impl-code, impl-docs)

    Returns:
        Current iteration (1 if file missing or no iteration field)
    """
    qr_state = load_qr_state(state_dir, phase)
    if not qr_state:
        return 1
    return qr_state.get("iteration", 1)


def has_qr_failures(state_dir: str, phase: str) -> bool:
    """Check if QR state has blocking failures at current iteration.

    Severity-aware via composable predicates: only FAIL items whose
    severity is in the blocking set for the current iteration count
    as failures. A phase with only below-threshold FAIL items returns
    False (no blocking failures), which means:
    - Work step routers do not enter fix mode
    - Gate step receives --qr-status pass
    - Below-threshold items remain FAIL in state (no auto-pass)

    Args:
        state_dir: Path to state directory
        phase: QR phase name (plan-design, plan-code, plan-docs, impl-code, impl-docs)

    Returns:
        True if qr-{phase}.json has FAIL items at blocking severity
    """
    qr_state = load_qr_state(state_dir, phase)
    if not qr_state:
        return False
    iteration = qr_state.get("iteration", 1)
    return len(query_items(qr_state, by_status("FAIL"), by_blocking_severity(iteration))) > 0


def qr_file_exists(state_dir: str, phase: str) -> bool:
    """Check if qr-{phase}.json exists (regardless of content).

    WHY existence check, not content validation:
    Decompose step checks existence to enforce single-run invariant;
    verify step validates content for pass/fail status. Conflating these
    checks would couple decomposition to verification state.

    WHY distinct from has_qr_failures():
    has_qr_failures() checks item status (pass/fail); this checks file
    existence. Decompose needs existence signal; route needs status signal.

    Args:
        state_dir: Path to state directory
        phase: QR phase name (plan-design, plan-code, plan-docs, impl-code, impl-docs)

    Returns:
        True if qr-{phase}.json exists, False otherwise
    """
    if not state_dir:
        return False
    path = Path(state_dir) / f"qr-{phase}.json"
    return path.exists()


def increment_qr_iteration(state_dir: str, phase: str) -> int | None:
    """Increment iteration counter in qr-{phase}.json.

    WHY verify step owns iteration increment:
    Iteration tracks verification cycles (decompose->verify->fix->verify),
    not decomposition invocations. Decompose always writes iteration=1;
    verify increments on RETRY after fixes applied.

    WHY lock-free is safe here (unlike the item writers): this is the sole
    writer of the `iteration` field and the orchestrator calls it at the
    verify-dispatch step in a single process, BEFORE the parallel verify agents
    fan out. The single-writer invariant is positional (run-before-fan-out), so
    no qr_write_lock is needed; the agents only mutate items[].

    WHY atomic write:
    atomic_write_text writes a unique temp file then os.replace()s it in, so a
    reader sees complete old-or-new state, never partial JSON.

    WHY returns None instead of raising:
    File may be deleted between decompose and verify (user intervention,
    disk issues). Returning None allows caller to handle gracefully;
    next iteration will run decompose fresh.

    Args:
        state_dir: Path to state directory
        phase: QR phase name

    Returns:
        New iteration value, or None if file doesn't exist
    """
    from skills.lib.io import atomic_write_text

    path = Path(state_dir) / f"qr-{phase}.json"
    if not path.exists():
        return None

    qr_state = json.loads(path.read_text())
    iteration = qr_state.get("iteration", 1) + 1
    qr_state["iteration"] = iteration
    atomic_write_text(path, json.dumps(qr_state, indent=2))
    return iteration


def get_pending_qr_items(state_dir: str, phase: str) -> list[str]:
    """Return item IDs that need processing (status TODO or FAIL).

    Args:
        state_dir: Path to state directory
        phase: QR phase name (plan-design, plan-code, plan-docs, impl-code, impl-docs)

    Returns:
        List of item IDs with TODO or FAIL status
    """
    qr_state = load_qr_state(state_dir, phase)
    if not qr_state:
        return []

    pending = []
    for item in qr_state.get("items", []):
        status = item.get("status")
        if status in ("TODO", "FAIL"):
            pending.append(item.get("id", ""))
    return [id for id in pending if id]
