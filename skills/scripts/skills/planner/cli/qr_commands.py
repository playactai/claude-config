"""QR state manipulation commands as plain functions.

Each public function with 'ctx' as first param is auto-discovered as RPC method.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from skills.planner.shared.qr.utils import qr_write_lock
from skills.planner.shared.schema import canonicalize_severity

from .qr_common import (
    FORBIDS_FINDING,
    REQUIRES_FINDING,
    TERMINAL_STATUSES,
    VALID_STATUSES,
    find_item,
    is_valid_group_id,
    load_qr_state_under_lock,
    save_qr_state_atomic,
)


@dataclass
class QRContext:
    """Context passed to all QR commands."""

    state_dir: Path
    phase: str

    def qr_path(self) -> Path:
        return self.state_dir / f"qr-{self.phase}.json"

    def state_file(self) -> Path:
        """Single mutable state file (used by batch snapshot/rollback)."""
        return self.qr_path()


def update_item(
    ctx: QRContext,
    item_id: str,
    status: str,
    finding: str | None = None,
    severity: str | None = None,
) -> dict:
    """Update QR item status with file locking."""
    status = status.upper()

    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}. Must be PASS or FAIL.")

    if status in REQUIRES_FINDING and not finding:
        raise ValueError(f"Status {status} requires finding to explain what failed.")

    if status in FORBIDS_FINDING and finding:
        raise ValueError(f"Status {status} forbids finding. PASS means no issues found.")

    if severity is not None:
        canonical = canonicalize_severity(severity)
        if canonical is None:
            raise ValueError(
                f"Invalid severity: {severity}. Must be MUST, SHOULD, or COULD "
                "(or BLOCKER/CRITICAL)."
            )
        severity = canonical

    qr_path = ctx.qr_path()
    if not qr_path.exists():
        raise FileNotFoundError(f"QR state file not found: {qr_path}")

    with qr_write_lock(ctx.state_dir, ctx.phase):
        qr_state = load_qr_state_under_lock(qr_path)

        idx, item = find_item(qr_state, item_id)
        if idx < 0:
            raise ValueError(f"Item {item_id} not found in qr-{ctx.phase}.json")
        assert item is not None

        current_status = item.get("status", "TODO")
        if current_status in TERMINAL_STATUSES:
            raise ValueError(
                f"Item {item_id} has terminal status {current_status}. "
                f"Cannot transition to {status}."
            )

        item["version"] = item.get("version", 1) + 1
        item["status"] = status
        if finding:
            item["finding"] = finding
        elif "finding" in item and status == "PASS":
            del item["finding"]
        if severity:
            item["severity"] = severity

        qr_state["items"][idx] = item
        save_qr_state_atomic(ctx.qr_path(), qr_state)

    return {"id": item_id, "version": item["version"], "operation": "updated"}


def get_item(ctx: QRContext, item_id: str) -> dict:
    """Get QR item by ID."""
    qr_path = ctx.qr_path()
    if not qr_path.exists():
        raise FileNotFoundError(f"QR state file not found: {qr_path}")

    with open(qr_path, encoding="utf-8") as f:
        qr_state = json.load(f)

    _, item = find_item(qr_state, item_id)
    if item is None:
        raise ValueError(f"Item {item_id} not found")

    return item


def list_items(ctx: QRContext, status: str | None = None) -> list[dict]:
    """List QR items, optionally filtered by status."""
    qr_path = ctx.qr_path()
    if not qr_path.exists():
        raise FileNotFoundError(f"QR state file not found: {qr_path}")

    with open(qr_path, encoding="utf-8") as f:
        qr_state = json.load(f)

    items = []
    for item in qr_state.get("items", []):
        item_status = item.get("status", "TODO")
        if status and item_status != status.upper():
            continue
        items.append(
            {
                "id": item.get("id"),
                "status": item_status,
                "finding": item.get("finding"),
            }
        )

    return items


def summary(ctx: QRContext) -> dict:
    """Get summary of QR state (counts by status)."""
    qr_path = ctx.qr_path()
    if not qr_path.exists():
        raise FileNotFoundError(f"QR state file not found: {qr_path}")

    with open(qr_path, encoding="utf-8") as f:
        qr_state = json.load(f)

    counts = {"TODO": 0, "PASS": 0, "FAIL": 0}
    for item in qr_state.get("items", []):
        status = item.get("status", "TODO")
        counts[status] = counts.get(status, 0) + 1

    return {
        "phase": ctx.phase,
        "total": sum(counts.values()),
        "counts": counts,
    }


def assign_group(ctx: QRContext, item_id: str, group_id: str) -> dict:
    """Assign QR item to a group."""
    if not is_valid_group_id(group_id):
        raise ValueError(
            f"Invalid group_id '{group_id}'. "
            f"Must be 'umbrella' or start with: parent-, component-, concern-, affinity-"
        )

    qr_path = ctx.qr_path()
    if not qr_path.exists():
        raise FileNotFoundError(f"QR state file not found: {qr_path}")

    with qr_write_lock(ctx.state_dir, ctx.phase):
        qr_state = load_qr_state_under_lock(qr_path)

        idx, item = find_item(qr_state, item_id)
        if idx < 0:
            raise ValueError(f"Item {item_id} not found in qr-{ctx.phase}.json")
        assert item is not None

        item["group_id"] = group_id
        qr_state["items"][idx] = item
        save_qr_state_atomic(ctx.qr_path(), qr_state)

    return {"id": item_id, "version": item.get("version", 1), "operation": "updated"}
