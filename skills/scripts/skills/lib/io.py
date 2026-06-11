"""Unified file I/O with contextual error handling."""

import os
import sys
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace ``path``'s contents with ``text``.

    The single atomic-write primitive for state files. Writes to a UNIQUE temp
    file in the same directory (via mkstemp -- never a shared fixed ".tmp" name
    that two concurrent writers would collide on) and renames it into place
    (atomic on POSIX: readers see old-or-new, never a half-written file).

    This does NOT lock: atomicity is per-write, not per-read-modify-write. A
    caller that must serialise a load -> mutate -> save cycle against concurrent
    writers holds its own lock around the cycle (see qr_write_lock for the
    qr-{phase}.json path). plan.json is single-writer, so it needs no lock.
    """
    path = Path(path)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def read_text_or_exit(path: Path, context: str) -> str:
    """Read file contents or exit with contextual error message.

    Args:
        path: Path to file to read
        context: Context string for error message (e.g., "loading convention")

    Returns:
        File contents as string

    Exits:
        With contextual error message if file not found or permission denied
    """
    try:
        return path.read_text()
    except FileNotFoundError:
        sys.exit(f"ERROR: {context}: file not found: {path}")
    except PermissionError:
        sys.exit(f"ERROR: {context}: permission denied: {path}")
