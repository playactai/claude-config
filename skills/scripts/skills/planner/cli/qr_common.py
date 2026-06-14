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
from skills.planner.shared.qr.utils import find_item as _find_item

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
    return json.loads(content)


def save_qr_state_atomic(qr_path: Path, qr_state: dict) -> None:
    """Write QR state atomically (unique temp + rename via the shared helper).

    Caller must hold the phase write lock across the read -> mutate -> save
    cycle: atomic_write_text gives per-write atomicity but no RMW exclusion.
    """
    atomic_write_text(qr_path, json.dumps(qr_state, indent=2))


def find_item(qr_state: dict, item_id: str) -> tuple[int, dict | None]:
    """Find item by ID. Returns (index, item) or (-1, None) if not found.

    Re-exported from the shared qr/utils layer; the two CLIs stay import-compatible.
    """
    return _find_item(qr_state, item_id)
