"""QR state utilities for item-level verification and fix workflows.

Consolidated from planner/shared/qr_utils.py.

Provides centralized access to qr-<phase>.json state files:
- load_qr_state: Parse QR state from state directory
- get_qr_item: Single item lookup by ID (for --qr-item verification)
- format_*: Prompt formatting for different workflows
"""

import contextlib
import fcntl
import hashlib
import json
import math
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from xml.sax.saxutils import escape

from skills.planner.shared.qr.types import LoopState, QRState, QRStatus
from skills.planner.shared.schema import (
    LINE_FORGING_ORDS,
    QA_ITEM_DEFAULTS,
    canonicalize_severity,
)

_HELD: dict[tuple[int, str], list] = {}  # (thread_id, lock_path) -> [depth, file]
_HELD_LOCK = threading.Lock()


@contextlib.contextmanager
def qr_write_lock(state_dir: str | Path, phase: str) -> Iterator[None]:
    """Serialize qr-{phase}.json writers on a stable sentinel inode.

    The data file is replaced via atomic rename on every write, so locking it
    directly provides NO mutual exclusion: a writer that blocks on flock()
    wakes holding a lock on the orphaned pre-rename inode and clobbers the
    writer that won the race (roughly half of concurrent writes are lost
    under load). Locking a sentinel file that is never renamed gives true
    exclusion, while the atomic rename of the data file still gives lock-free
    readers (summary/list/get and the router's detect_qr_state()) an
    all-or-nothing view.

    Hold this lock across the full read -> mutate -> atomic-write cycle.
    Re-acquire within one process and same thread is re-entrant (so a batch
    can hold it around per-item writers without self-deadlock). Cross-process
    exclusion is preserved: each process starts at depth 0 and acquires the
    real flock. Concurrent threads each acquire the flock independently.
    """
    lock_path = str((Path(state_dir) / f"qr-{phase}.lock").resolve())
    key = (threading.get_ident(), lock_path)
    with _HELD_LOCK:
        held = _HELD.get(key)
    if held is not None:
        # Re-entrant acquire on the same thread: the flock is already held, so
        # just track nesting depth (this lets a batch wrap per-item writers
        # without self-deadlock). Only this thread owns this key, so the
        # unlocked depth bump is race-free.
        held[0] += 1
        try:
            yield
        finally:
            held[0] -= 1
        return
    # First acquire on this thread: take the real flock BEFORE registering the
    # entry, so a flock failure (EINTR, NFS error) leaves no poisoned _HELD entry
    # -- which would make later acquires on this thread skip locking and run with
    # no exclusion -- and leaks no fd.
    f = open(lock_path, "a")  # noqa: SIM115
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    except BaseException:
        f.close()
        raise
    held = [1, f]
    with _HELD_LOCK:
        _HELD[key] = held
    try:
        yield
    finally:
        with _HELD_LOCK:
            held[0] -= 1
            done = held[0] == 0
            if done:
                del _HELD[key]
        if done:
            f.close()


def _validate_qr_item_shape(item: object) -> None:
    """Reject an items[] entry whose identity/relational fields would crash a direct
    (validate_state-skipping) consumer. `id` is required+str: it is find_item's lookup
    key, a `{i["id"]}` set member at decompose step 9, and a shlex.quote() arg in
    build_qr_verify_dispatch (absent -> KeyError, unhashable -> TypeError). status/
    group_id/parent_id are str-or-absent: status keys status_counts' dict and by_status'
    frozenset; group_id keys decompose's groups dict; parent_id is tested `in item_ids`.
    Matches QRItem's str / str|None typing for exactly these fields -- the free-text
    fields (scope/check/finding) are str()-wrapped at their sinks and the numeric fields
    (version/iteration) are _coerce_*'d, so neither is checked here."""
    if not isinstance(item, dict):
        raise ValueError("qr state item is not an object")
    if not isinstance(item.get("id"), str):
        raise ValueError("qr state item id is not a string")
    for field in ("status", "group_id", "parent_id"):
        value = item.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"qr state item {field} is not a string")


def parse_qr_dict(content: str) -> dict:
    """Parse qr-file text into a dict; raise ValueError if it violates the minimal shape
    every direct QR consumer relies on.

    The shared dict-contract core for the two QR loaders. The orchestrator gates its own
    reads through QRFile/validate_state, but the direct readers -- the CLI (status_counts,
    filtered_items_view), the router (detect_qr_state -> by_status), the fix subprocess
    (format_failed_items_for_fix), and decompose steps 9/13 ({i["id"] for i in items},
    groups.setdefault(group_id), parent_id `in` set) -- bypass that gate. They iterate
    items[] and use the identity fields as dict keys / set members / shlex.quote args, so a
    structurally-malformed-but-top-level-dict file (items not a list, a non-object item, an
    unhashable/absent id, a list-valued status/group_id/parent_id) would crash them with a
    raw TypeError/KeyError/AttributeError -- a raw traceback instead of the clean
    <qr_cli_error> frame. Validate the shape once here (the single load chokepoint) rather
    than at every consumer, mirroring how _coerce_positive_int hardened the raw int fields
    one layer up.

    Each loader layers its own policy on the raised ValueError: load_qr_state swallows
    everything to None (fail closed); load_qr_state_under_lock surfaces the message so the
    CLI's top-level handler renders a clean error frame instead of a raw traceback.
    """
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("qr state is not a JSON object")
    items = data.get("items")
    if items is not None:
        if not isinstance(items, list):
            raise ValueError("qr state items is not a list")
        for item in items:
            _validate_qr_item_shape(item)
    return data


def load_qr_state(state_dir: str, phase: str) -> dict | None:
    """Load and parse qr-<phase>.json from state directory.

    Args:
        state_dir: Path to state directory
        phase: QR phase name (plan-design, impl-code, impl-docs)

    Returns:
        Parsed QR state dict, or None if the file is missing, unparseable, or
        not a JSON object
    """
    path = Path(state_dir) / f"qr-{phase}.json"
    if not path.exists():
        return None

    # A valid-JSON file that isn't an object (e.g. a decompose scratch list)
    # violates the dict contract every caller relies on -- return None so the gate
    # fails closed (and `.get`-based callers don't crash) instead of treating an
    # unconfirmable QR file as present. Truncated/unreadable files fail closed too.
    try:
        return parse_qr_dict(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def load_validated_qr_state(state_dir: str, phase: str) -> dict | None:
    """Load qr-{phase}.json and re-run QRFile validation, failing closed to None.

    The orchestrator's validate_state gate runs only in the orchestrator process; the
    decompose/verify subprocesses re-read the file directly (load_qr_state checks
    dict-ness only), so a control-char-forged id/scope -- a column-0 prompt-injection
    vector -- must be rejected here too. Single home for that boundary, shared by both
    subprocess entry points.
    """
    from pydantic import ValidationError

    from skills.planner.shared.schema import QRFile

    qr_state = load_qr_state(state_dir, phase)
    if qr_state is None:
        return None
    try:
        QRFile.model_validate(qr_state)
    except ValidationError:
        return None
    return qr_state


def get_qr_item(qr_state: dict, item_id: str) -> dict | None:
    """Get single QR item by ID.

    Args:
        qr_state: Parsed QR state from load_qr_state()
        item_id: Item ID (e.g., "plan-001")

    Returns:
        Item dict or None if not found
    """
    return find_item(qr_state, item_id)[1] if qr_state else None


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

    Severity is canonicalized before the membership test (via
    canonicalize_severity) so a decompose agent that writes lower-case
    "must"/"should" is not downgraded, and the high-severity synonyms
    "BLOCKER"/"CRITICAL" block like MUST -- matching schema._normalize_severity.
    A genuinely-unknown token defaults to SHOULD rather than being silently
    treated as non-blocking.
    """
    from skills.planner.shared.qr.constants import get_blocking_severities

    blocking = get_blocking_severities(iteration)

    def _coerce(raw: object) -> str:
        # Single severity chokepoint (schema.canonicalize_severity): lower-case
        # -> canonical, BLOCKER/CRITICAL -> MUST, unknown/empty -> SHOULD.
        return canonicalize_severity(raw) or "SHOULD"

    return lambda item: _coerce(item.get("severity")) in blocking


def _coerce_positive_int(raw: object, default: int = 1) -> int:
    """qr-{phase}.json is external; a string/float/garbled int must not crash callers
    that do severity math (gate's `iteration >= 4`) or arithmetic (version bumps).
    Single chokepoint for every integer field read raw off the external file.
    Tolerant: non-int or below `default` -> `default`. OverflowError is caught because
    json.loads accepts the bare `Infinity`/`-Infinity` tokens, and int(float('inf'))
    raises OverflowError (not ValueError, unlike nan)."""
    try:
        n = int(raw)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError, OverflowError):
        return default
    return n if n >= default else default


def _coerce_iteration(raw: object) -> int:
    """qr-{phase}.json iteration: non-int -> 1 (see _coerce_positive_int)."""
    return _coerce_positive_int(raw, default=1)


def _iteration_of(qr_state: dict | None) -> int:
    return _coerce_iteration(qr_state.get("iteration")) if qr_state else 1


def _blocking_items_from_state(qr_state: dict | None, *statuses: str) -> list[dict]:
    """Return items at any of *statuses whose severity blocks at the current iteration.

    Operates on a pre-loaded qr_state dict. Backs has_qr_failures_from_state
    (statuses="FAIL") and the gate's TODO veto (_has_blocking_todo_from_state), so a
    change to iteration default or severity handling applies to both.
    """
    if not qr_state:
        return []
    iteration = _iteration_of(qr_state)
    return query_items(qr_state, by_status(*statuses), by_blocking_severity(iteration))


def query_items(qr_state: dict, *predicates: ItemPredicate) -> list[dict]:
    """Filter items by composable predicates applied conjunctively.

    Predicates compose via logical AND: an item is included only if
    all predicates return True. With zero predicates, returns all
    items (identity filter).

    Applies policy filters (status + severity thresholds) for workflow
    decisions; compose with by_status()/by_blocking_severity(). Display
    code that just needs a status filter uses by_status() directly.

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


def find_item(qr_state: dict, item_id: str) -> tuple[int, dict | None]:
    """Find item by ID. Returns (index, item) or (-1, None) if not found.

    Shared implementation; qr_common re-exports this for CLI callers.
    """
    for i, item in enumerate(qr_state.get("items", [])):
        if item.get("id") == item_id:
            return i, item
    return -1, None


def balance_verify_groups(
    items: list[dict],
    *,
    max_parallel: int,
    target_per_group: int,
) -> list[list[dict]]:
    """Re-bin verify items into balanced, capped parallel groups.

    The decompose agent's group_id is an affinity hint, not a correctness
    boundary -- every item carries its own independent check and PASS/FAIL, so
    which agent verifies an item is free to change. Dispatching one agent per raw
    group_id leaves two failure modes: a fat group serializes many items while
    siblings idle, or every item becomes its own agent (N x the fixed per-agent
    context-load cost).

    Items are sorted by (group_id or id) -- keeping affinity-clustered items
    adjacent -- then split into k = min(max_parallel, ceil(n / target_per_group))
    contiguous near-equal chunks (sizes differ by at most 1). This guarantees both
    bounds at once: at most max_parallel groups, and a max group size of
    ceil(n / k); affinity adjacency survives except at chunk seams.

    Returns a list of non-empty item groups ([] when there are no items).
    """
    if not items:
        return []
    per = max(1, target_per_group)
    cap = max(1, max_parallel)
    ordered = sorted(items, key=lambda it: str(it.get("group_id") or it.get("id") or ""))
    n = len(ordered)
    k = min(cap, max(1, math.ceil(n / per)))  # 1 <= k <= n, so no chunk is empty
    base, extra = divmod(n, k)  # the first `extra` chunks take one more item
    groups: list[list[dict]] = []
    start = 0
    for i in range(k):
        size = base + (1 if i < extra else 0)
        groups.append(ordered[start : start + size])
        start += size
    return groups


def format_qr_item_for_verification(item: dict) -> str:
    """Format single QR item for verification prompt.

    Used by QR scripts when invoked with --qr-item to verify one item.
    """
    if not item:
        return "ERROR: Item not found"

    lines = [
        "<qr_item_to_verify>",
        f"  <id>{escape(str(item.get('id', QA_ITEM_DEFAULTS['id'])))}</id>",
        f"  <scope>{escape(str(item.get('scope', QA_ITEM_DEFAULTS['scope'])))}</scope>",
        # check is free-text (not control-char-validated in schema).  escape()
        # neutralises <>& but not line-breaking characters — a \n/\r/NEL/LS/PS
        # in check would forge a column-0 instruction line in the ANALYZE/CONFIRM
        # verify prompt.  Collapse every LINE_FORGING_ORDS to ⏎ so the XML stays
        # single-line; the schema's _reject_control_chars covers id/scope (identity
        # fields), while the remaining plaintext sinks use _fix_field_safe.
        f"  <check>{escape(''.join('⏎' if ord(c) in LINE_FORGING_ORDS else c for c in str(item.get('check', QA_ITEM_DEFAULTS['check']))))}</check>",
        "</qr_item_to_verify>",
        "",
        "VERIFY this specific item. Return exactly:",
        "  PASS - if check passes",
        "  FAIL - if check fails, with finding explaining why",
    ]
    return "\n".join(lines)


def _fix_field_safe(text: object) -> str:
    """Make an untrusted QR field safe to interpolate into the PLAINTEXT fixer
    prompt: replace every line-breaking char (LINE_FORGING_ORDS) with a space,
    except a legitimate newline, which is kept and its continuation indented -- so
    an embedded line break can't forge a column-0 instruction line.

    check/finding are free-text and are NOT control-char validated (and finding is
    written by a verify sub-agent), and the fix subprocess re-reads the file without
    the orchestrator's validate_state gate -- so neutralize at this sink. Multiline
    content is kept, just visibly nested under its item.
    """
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    s = "".join(" " if ord(c) in LINE_FORGING_ORDS and c != "\n" else c for c in s)
    return s.replace("\n", "\n      ")


def format_failed_items_for_fix(qr_state: dict) -> str:
    """Format the blocking-severity failed items for the fixer prompt.

    Used by developer/architect/TW fix scripts when QR failures detected. Filters
    to the same blocking set the gate routes on (FAIL items whose severity blocks at
    the state's current iteration), so the fixer is told to address exactly the items
    that keep the gate failing -- not de-escalated FAILs the gate now lets pass.
    """
    failed = _blocking_items_from_state(qr_state, "FAIL")
    if not failed:
        return ""

    lines = [
        "=" * 60,
        "FAILED QR ITEMS TO FIX (address these FIRST):",
        "=" * 60,
        "",
    ]
    for item in failed:
        lines.append(f"[{_fix_field_safe(item.get('id', '?'))}] {_fix_field_safe(item.get('check', ''))}")
        if item.get("scope") and item.get("scope") != "*":
            lines.append(f"    Scope: {_fix_field_safe(item['scope'])}")
        if item.get("finding"):
            lines.append(f"    Finding: {_fix_field_safe(item['finding'])}")
        lines.append("")

    lines.append("=" * 60)
    lines.append("")
    return "\n".join(lines)


def get_qr_iteration(state_dir: str, phase: str) -> int:
    """Get current QR iteration from qr-{phase}.json.

    Args:
        state_dir: Path to state directory
        phase: QR phase name (plan-design, impl-code, impl-docs)

    Returns:
        Current iteration (1 if file missing or no iteration field)
    """
    qr_state = load_qr_state(state_dir, phase)
    if not qr_state:
        return 1
    return _iteration_of(qr_state)


def get_qr_iteration_from_state(qr_state: dict | None) -> int:
    """Same as get_qr_iteration but accepts a pre-loaded qr_state dict (or None)."""
    return _iteration_of(qr_state)


def has_qr_failures_from_state(qr_state: dict) -> bool:
    """True if the pre-loaded qr_state has FAIL items at blocking severity for the
    current iteration.

    Severity-aware: only FAIL items whose severity is in the blocking set for the
    current iteration count. A phase with only below-threshold FAIL items returns
    False -- work-step routers do not enter fix mode, the gate receives a pass, and
    the below-threshold items remain FAIL in state (no auto-pass).
    """
    return len(_blocking_items_from_state(qr_state, "FAIL")) > 0


def resolve_qr_for_step(
    qr_states: dict | None,
    state_dir: str | None,
    phase: str | None,
    qr_status: str | None,
) -> tuple[dict | None, QRState]:
    """Resolve the QR state dict + QRState for one orchestrator step.

    Both orchestrators derive the same per-step QR context: prefer a preloaded
    qr_states model (the batch path, which avoids a second disk load) else read
    qr-{phase}.json. Iteration and fix-mode come from the state itself; status from
    the gate flag. The caller passes the resolved phase (executor maps step->phase
    via EXECUTOR_STEP_PHASES; planner reads it off the step handler).
    """
    if qr_states is not None:
        qr_model = qr_states.get(phase) if state_dir and phase else None
        qr_state = qr_model.model_dump(mode="json") if qr_model else None
    else:
        qr_state = load_qr_state(state_dir, phase) if state_dir and phase else None
    iteration = get_qr_iteration_from_state(qr_state) if qr_state else 1
    status = QRStatus(qr_status) if qr_status else None
    fix_mode = bool(qr_state and has_qr_failures_from_state(qr_state))
    state = LoopState.RETRY if fix_mode else LoopState.INITIAL
    qr = QRState(iteration=iteration, state=state, status=status)
    return qr_state, qr


def qr_file_exists(state_dir: str, phase: str) -> bool:
    """Check if qr-{phase}.json exists (regardless of content).

    WHY existence check, not content validation:
    Decompose step checks existence to enforce single-run invariant;
    verify step validates content for pass/fail status. Conflating these
    checks would couple decomposition to verification state.

    WHY distinct from has_qr_failures_from_state():
    has_qr_failures_from_state() checks item status (pass/fail); this checks file
    existence. Decompose needs existence signal; route needs status signal.

    Args:
        state_dir: Path to state directory
        phase: QR phase name (plan-design, impl-code, impl-docs)

    Returns:
        True if qr-{phase}.json exists, False otherwise
    """
    if not state_dir:
        return False
    path = Path(state_dir) / f"qr-{phase}.json"
    return path.exists()


def _fail_signature(items: list[dict]) -> str | None:
    """Stable fingerprint of the recorded FAIL set (id + version), or None if empty.

    The idempotency key for the RETRY iteration bump. update-item bumps an item's
    `version` on every write (and FAIL->FAIL is a legal re-verify), so a genuine
    verify->fix->verify cycle always changes the fingerprint, while a transient
    verify-step re-render with the same on-disk FAILs recomputes the same value and
    the bump is skipped -- one fix cycle counts once. Returns None when nothing is
    recorded as FAIL, so the bump never fires without a recorded blocking FAIL
    (the gate's documented invariant).
    """
    fails = sorted(
        (it.get("id", ""), it.get("version", 1))
        for it in items
        if it.get("status") == "FAIL"
    )
    if not fails:
        return None
    return hashlib.sha256(json.dumps(fails).encode("utf-8")).hexdigest()


def increment_qr_iteration(state_dir: str, phase: str, sig: str) -> int | None:
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
        sig: Fingerprint of the FAIL set this bump is for (_fail_signature),
            persisted as iteration_sig so a re-render with the same FAILs is a
            no-op (the idempotency guard lives in prepare_verify_items).

    Returns:
        New iteration value, or None if file doesn't exist
    """
    from skills.lib.io import atomic_write_text

    path = Path(state_dir) / f"qr-{phase}.json"
    if not path.exists():
        return None

    qr_state = json.loads(path.read_text(encoding="utf-8"))
    iteration = _iteration_of(qr_state) + 1
    qr_state["iteration"] = iteration
    qr_state["iteration_sig"] = sig
    atomic_write_text(path, json.dumps(qr_state, indent=2))
    return iteration


def prepare_verify_items(
    state_dir: str,
    phase: str,
    qr,
    qr_state: dict | None = None,
) -> tuple[list[dict] | None, int]:
    """Load QR state and return (items, iteration) for the verify step.

    When qr_state is provided the disk read is skipped (caller already loaded it).
    Returns (None, 1) when qr-{phase}.json is missing or malformed so callers
    can route-to-decompose (executor) or return an error dict (planner).
    """
    from skills.planner.shared.qr.types import LoopState

    if qr_state is None:
        qr_state = load_qr_state(state_dir, phase)
    if not qr_state or "items" not in qr_state:
        return None, 1
    iteration = _iteration_of(qr_state)
    if qr.state == LoopState.RETRY:
        # Idempotent bump: advance only when the recorded FAIL set changed since
        # the last bump (a genuine new fix cycle). A transient verify re-render
        # with the same FAILs recomputes the same signature and skips -- one fix
        # cycle counts once. sig is None when nothing is recorded FAIL, so the bump
        # never fires without one (gate invariant: "iteration advances only on a
        # recorded blocking FAIL").
        sig = _fail_signature(qr_state.get("items", []))
        if sig is not None and qr_state.get("iteration_sig") != sig:
            new_iter = increment_qr_iteration(state_dir, phase, sig)
            if new_iter is not None:
                iteration = new_iter
    return query_items(qr_state, by_status("TODO", "FAIL"), by_blocking_severity(iteration)), iteration
