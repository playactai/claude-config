"""QR state manipulation commands as plain functions.

Each public function with 'ctx' as first param is auto-discovered as RPC method.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from skills.planner.shared.qr.utils import load_qr_state, qr_write_lock
from skills.planner.shared.schema import canonicalize_severity

from .qr_common import (
    assign_group_in_state,
    filtered_items_view,
    find_item,
    is_valid_group_id,
    load_qr_state_under_lock,
    save_qr_state_atomic,
    status_counts,
    update_item_in_state,
)


@dataclass
class QRContext:
    """Context passed to all QR commands."""

    state_dir: Path
    phase: str
    _batch: dict | None = None
    _batch_dirty: bool = False

    def qr_path(self) -> Path:
        return self.state_dir / f"qr-{self.phase}.json"

    def state_file(self) -> Path:
        """Single mutable state file (used by batch snapshot/rollback)."""
        return self.qr_path()

    def batch_lock(self):
        """Re-entrant write lock for holding across batch snapshot+loop+restore."""
        return qr_write_lock(self.state_dir, self.phase)

    def begin_batch(self) -> None:
        self._batch = self.load_qr_state()
        self._batch_dirty = False

    def end_batch(self) -> None:
        self._batch = None
        self._batch_dirty = False

    def load_qr_state(self) -> dict:
        if self._batch is not None:
            return self._batch
        qr_path = self.qr_path()
        return load_qr_state_under_lock(qr_path)

    def read_qr_state(self) -> dict | None:
        """Cache-aware read for read-only commands.

        Returns the in-batch cache when a batch is active, so a read sees writes
        cached earlier in the same batch (the write commands cache, not persist,
        until flush_batch). Outside a batch it is a lock-free disk read that
        returns None on a missing/corrupt file, matching the read commands'
        existing error handling.
        """
        if self._batch is not None:
            return self._batch
        return load_qr_state(str(self.state_dir), self.phase)

    def save_qr_state(self, qr_state: dict) -> None:
        if self._batch is not None:
            self._batch = qr_state
            self._batch_dirty = True
            return
        save_qr_state_atomic(self.qr_path(), qr_state)

    def flush_batch(self) -> None:
        # Persist once, and only if a command actually mutated the cache -- a
        # read-only batch must not rewrite the state file.
        if self._batch is not None and self._batch_dirty:
            save_qr_state_atomic(self.qr_path(), self._batch)
        self._batch = None
        self._batch_dirty = False


def update_item(
    ctx: QRContext,
    item_id: str,
    status: str,
    finding: str | None = None,
    severity: str | None = None,
) -> dict:
    """Update QR item status with file locking."""
    status = status.upper()

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
        qr_state = ctx.load_qr_state()
        item = update_item_in_state(qr_state, item_id, status, finding, severity)
        ctx.save_qr_state(qr_state)

    return {"id": item_id, "version": item["version"], "operation": "updated"}


def get_item(ctx: QRContext, item_id: str) -> dict:
    """Get QR item by ID."""
    qr_path = ctx.qr_path()
    if not qr_path.exists():
        raise FileNotFoundError(f"QR state file not found: {qr_path}")

    qr_state = ctx.read_qr_state()
    if qr_state is None:
        raise ValueError(f"{qr_path.name} is not a valid QR state object")

    _, item = find_item(qr_state, item_id)
    if item is None:
        raise ValueError(f"Item {item_id} not found")

    return item


def list_items(ctx: QRContext, status: str | None = None) -> list[dict]:
    """List QR items, optionally filtered by status."""
    qr_path = ctx.qr_path()
    if not qr_path.exists():
        raise FileNotFoundError(f"QR state file not found: {qr_path}")

    qr_state = ctx.read_qr_state()
    if qr_state is None:
        raise ValueError(f"{qr_path.name} is not a valid QR state object")

    return filtered_items_view(qr_state, status.upper() if status else None)


def summary(ctx: QRContext) -> dict:
    """Get summary of QR state (counts by status)."""
    qr_path = ctx.qr_path()
    if not qr_path.exists():
        raise FileNotFoundError(f"QR state file not found: {qr_path}")

    qr_state = ctx.read_qr_state()
    if qr_state is None:
        raise ValueError(f"{qr_path.name} is not a valid QR state object")

    counts = status_counts(qr_state)
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
        qr_state = ctx.load_qr_state()
        item = assign_group_in_state(qr_state, item_id, group_id)
        ctx.save_qr_state(qr_state)

    return {"id": item_id, "version": item.get("version", 1), "operation": "updated"}
