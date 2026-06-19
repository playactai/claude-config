"""Shared QR-CLI state primitives for qr.py and qr_commands.py.

Both the live CLI (qr.py, ``__main__``) and the batch-RPC library
(qr_commands.py, ``QRContext``) run the same lock -> read -> mutate ->
atomic-write cycle over qr-{phase}.json. This module holds the pieces that were
byte-identical across them -- the status frozensets, the group-id predicate, and
the path-based RMW helpers -- so the two entry points cannot drift. Each caller
keeps its own failure mode (qr.py's error_exit vs qr_commands' raise) and derives
the qr_path from its own state handle (state_dir+phase vs QRContext.qr_path()),
which is why the helpers take a Path rather than either caller's state object.
"""

from __future__ import annotations

import json
from pathlib import Path

from skills.lib.io import atomic_write_text
from skills.planner.shared.qr.utils import _coerce_positive_int, _fix_field_safe, parse_qr_dict
from skills.planner.shared.qr.utils import find_item as find_item

# Valid status values (match QAItemStatus enum)
VALID_STATUSES = frozenset({"PASS", "FAIL"})
# Terminal statuses that cannot be changed (PASS is terminal; no un-pass)
TERMINAL_STATUSES = frozenset({"PASS"})
# Statuses that require a finding
REQUIRES_FINDING = frozenset({"FAIL"})
# Statuses that forbid a finding
FORBIDS_FINDING = frozenset({"PASS"})

# Group-id prefixes assign-group accepts besides the bare "umbrella".
_GROUP_ID_PREFIXES = ("parent-", "component-", "concern-", "affinity-")


def is_valid_group_id(group_id: str) -> bool:
    """True when group_id is the bare 'umbrella' or carries a known prefix.

    Pure predicate so each caller keeps its own failure mode (qr.py error_exit,
    qr_commands raise) and its own human-facing message listing the prefixes.
    """
    return group_id == "umbrella" or group_id.startswith(_GROUP_ID_PREFIXES)


def load_qr_state_under_lock(qr_path: Path) -> dict:
    """Read QR state from qr_path. Caller must hold the phase write lock."""
    content = qr_path.read_text(encoding="utf-8") if qr_path.exists() else ""
    if not content:
        return {"phase": "", "items": []}
    try:
        return parse_qr_dict(content)
    except json.JSONDecodeError as e:
        # Corrupt/truncated JSON: re-raise as ValueError carrying the filename
        # and parse location so the CLI's top-level handler emits a clean
        # <qr_cli_error> frame (message self-identifies) instead of a raw
        # traceback, without mislabeling it as a non-dict.
        raise ValueError(f"{qr_path.name} is not valid JSON: {e}") from e
    except ValueError as e:
        # parse_qr_dict raises a self-identifying ValueError for every dict-contract
        # violation (non-object, items-not-a-list, non-object item, bad identity field).
        # Surface its specific message so the clean <qr_cli_error> frame is accurate
        # instead of always labeling it "is not a JSON object".
        raise ValueError(f"{qr_path.name}: {e}") from e


def save_qr_state_atomic(qr_path: Path, qr_state: dict) -> None:
    """Write QR state atomically (unique temp + rename via the shared helper).

    Caller must hold the phase write lock across the read -> mutate -> save
    cycle: atomic_write_text gives per-write atomicity but no RMW exclusion.
    """
    atomic_write_text(qr_path, json.dumps(qr_state, indent=2))


# find_item is re-exported above (explicit ``as`` so pyflakes keeps the re-export
# rather than pruning it) so both CLIs share the one object via qr_common and
# cannot drift apart.


def update_item_in_state(qr_state: dict, item_id: str, status: str,
                         finding: str | None, severity: str | None) -> dict:
    """Validate + apply one status update to qr_state in place; return the item.

    Caller holds the phase write lock and owns severity canonicalization (pre-lock).
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}. Must be PASS or FAIL.")
    if status in REQUIRES_FINDING and not finding:
        raise ValueError(f"Status {status} requires a finding to explain what failed.")
    if status in FORBIDS_FINDING and finding:
        raise ValueError(f"Status {status} forbids a finding. PASS means no issues found.")
    idx, item = find_item(qr_state, item_id)
    if idx < 0:
        raise ValueError(f"Item {item_id} not found in qr-{qr_state.get('phase', '')}.json")
    assert item is not None
    current_status = item.get("status", "TODO")
    if current_status in TERMINAL_STATUSES:
        raise ValueError(
            f"Item {item_id} has terminal status {current_status}. Cannot transition to {status}."
        )
    # version is read raw off the external qr-{phase}.json (parse_qr_dict does no
    # type coercion); coerce before the bump so a string/garbled version cannot raise
    # an uncaught TypeError on the verify-record path -- same guard _coerce_iteration
    # already applies to the sibling iteration field.
    item["version"] = _coerce_positive_int(item.get("version", 1)) + 1
    item["status"] = status
    if finding:
        item["finding"] = finding
    elif "finding" in item and status == "PASS":
        del item["finding"]
    if severity:
        item["severity"] = severity
    qr_state["items"][idx] = item
    return item


def assign_group_in_state(qr_state: dict, item_id: str, group_id: str) -> dict:
    """Apply a group assignment in place; return the item. No version bump (metadata).

    group_id validity is checked by each caller before the file-exists check.
    """
    idx, item = find_item(qr_state, item_id)
    if idx < 0:
        raise ValueError(f"Item {item_id} not found in qr-{qr_state.get('phase', '')}.json")
    assert item is not None
    item["group_id"] = group_id
    qr_state["items"][idx] = item
    return item


def filtered_items_view(qr_state: dict, status_filter: str | None = None) -> list[dict]:
    """Status-filtered {id, status, finding} rows shared by the live CLI and batch RPC.

    Single sink for displaying findings: each finding is run through _fix_field_safe so
    a decompose-authored control character cannot forge a line in whatever prompt the
    row is rendered into (the schema's "neutralize finding at every sink" contract). The
    two list-items entry points (qr.py cmd_list_items, qr_commands.py list_items) share
    this so they cannot drift on sanitization.
    """
    rows = []
    for item in qr_state.get("items", []):
        item_status = item.get("status", "TODO")
        if status_filter and item_status != status_filter:
            continue
        finding = item.get("finding")
        rows.append({
            "id": item.get("id"),
            "status": item_status,
            "finding": _fix_field_safe(finding) if finding else None,
        })
    return rows


def status_counts(qr_state: dict) -> dict[str, int]:
    """{TODO, PASS, FAIL} counts shared by qr.py cmd_summary and qr_commands.py summary.

    One owner for the status-tally loop so the two summary entry points cannot drift.
    """
    counts: dict[str, int] = {"TODO": 0, "PASS": 0, "FAIL": 0}
    for item in qr_state.get("items", []):
        status = item.get("status", "TODO")
        counts[status] = counts.get(status, 0) + 1
    return counts
