"""Pure helpers for locating and reading Claude Code transcript JSONL files.

No prompt content here. Importable from tests. Mirrors the conventions
documented in skills/cc-history/SKILL.md.
"""

from __future__ import annotations

import glob as _glob
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

_RELATIVE_SINCE = re.compile(r"^(\d+)([hdwm])$")
_UNIT_SECONDS = {
    "h": 3600,
    "d": 86_400,
    "w": 7 * 86_400,
    "m": 30 * 86_400,
}


def _mtime_or_zero(p: Path) -> float:
    """Return p.stat().st_mtime, or 0.0 if the file disappeared since glob.

    Transcript files can rotate between glob() and the sort-key stat() call
    during heavy use; missing files sort last (oldest) so they fall out of
    "most-recent" lookups instead of crashing the skill with FileNotFoundError.
    """
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def encode_cwd(cwd: Path) -> str:
    """Encode a working directory path into the project-dir folder name.

    Rules (from cc-history/SKILL.md):
      - Leading "/" -> "-"
      - "/." (hidden directory) -> "--"
      - Regular "/" -> "-"

    Examples:
        /Users/bill/.claude       -> -Users-bill--claude
        /home/x/gitrepos/foo      -> -home-x-gitrepos-foo
    """
    s = str(cwd)
    if s.startswith("/"):
        s = "-" + s[1:]
    s = s.replace("/.", "--")
    s = s.replace("/", "-")
    return s


def get_project_dir(cwd: Path, projects_root: Path | None = None) -> Path:
    """Return the per-project transcript directory for cwd."""
    root = projects_root if projects_root is not None else DEFAULT_PROJECTS_ROOT
    return root / encode_cwd(cwd)


def find_active_project_dir(projects_root: Path | None = None) -> Path | None:
    """Locate the project dir hosting the most-recently-modified transcript.

    Used when CLAUDE_PROJECT_DIR isn't exposed and the caller can't supply --cwd.
    The active session's JSONL is being written to right now, so its mtime is
    strictly greater than any other transcript's.

    Returns None if no transcripts exist anywhere under projects_root.
    """
    root = projects_root if projects_root is not None else DEFAULT_PROJECTS_ROOT
    if not root.exists():
        return None
    candidates = sorted(
        root.glob("*/*.jsonl"),
        key=lambda p: (-_mtime_or_zero(p), str(p)),
    )
    return candidates[0].parent if candidates else None


def find_session_across_projects(
    session_id: str,
    projects_root: Path | None = None,
) -> Path | None:
    """Locate <projects_root>/*/<session_id>.jsonl across all project dirs.

    Used when the caller pinned a session UUID but didn't say which project
    hosts it (e.g., `/retrospective --session-id <uuid>` from a different
    repo). On the rare double-match (same UUID under two project dirs after
    a manual move), the most-recently-modified one wins.

    Returns None if no project hosts that uuid, the projects root itself is
    missing, or the session_id contains a path separator (defense in depth
    — a bare UUID has none).

    Glob metacharacters in session_id are escaped before pattern building,
    so `--session-id "*"` does NOT silently match every transcript.
    """
    if "/" in session_id or "\\" in session_id:
        return None
    root = projects_root if projects_root is not None else DEFAULT_PROJECTS_ROOT
    if not root.exists():
        return None
    safe_id = _glob.escape(session_id)
    matches = sorted(
        root.glob(f"*/{safe_id}.jsonl"),
        key=lambda p: (-_mtime_or_zero(p), str(p)),
    )
    return matches[0].parent if matches else None


def find_current_session(
    project_dir: Path,
    session_id: str | None = None,
) -> Path | None:
    """Resolve the JSONL transcript for the current session.

    With session_id: returns <project_dir>/<session_id>.jsonl if it exists.
    Without: returns the most-recently-modified *.jsonl in project_dir.
    Returns None if nothing matches.
    """
    if not project_dir.exists():
        return None

    if session_id:
        candidate = project_dir / f"{session_id}.jsonl"
        return candidate if candidate.is_file() else None

    candidates = sorted(
        project_dir.glob("*.jsonl"),
        key=lambda p: (-_mtime_or_zero(p), str(p)),
    )
    return candidates[0] if candidates else None


def parse_since(spec: str, now: datetime | None = None) -> datetime | None:
    """Parse a --since spec into a UTC datetime cutoff.

    Forms:
      "all"                  -> None (no filter)
      "24h" / "7d" / "1w"    -> now - duration
      ISO8601 ("2026-05-06T12:00:00Z" or with offset) -> parsed datetime

    Raises ValueError on malformed input.
    """
    spec = spec.strip()
    if spec == "all" or spec == "":
        return None

    m = _RELATIVE_SINCE.fullmatch(spec)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        seconds = n * _UNIT_SECONDS[unit]
        ref = now if now is not None else datetime.now(UTC)
        try:
            return ref - timedelta(seconds=seconds)
        except OverflowError as e:
            raise ValueError(f"Invalid --since: {spec!r} (out of range)") from e

    iso = spec.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as e:
        raise ValueError(f"Invalid --since: {spec!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def iter_messages(
    jsonl_path: Path,
    since: datetime | None = None,
) -> Iterator[dict]:
    """Yield parsed JSONL message objects, optionally filtered by timestamp.

    Malformed lines are skipped silently — transcripts can contain partial
    writes at the tail and we don't want a single bad line to block analysis.

    When since is set, messages without a parseable string timestamp are
    dropped (rather than silently passing through), so --since N can never
    return arbitrary-age messages.
    """
    with jsonl_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None:
                ts = msg.get("timestamp")
                if not isinstance(ts, str):
                    continue
                iso = ts.replace("Z", "+00:00")
                try:
                    msg_dt = datetime.fromisoformat(iso)
                except ValueError:
                    continue
                if msg_dt < since:
                    continue
            yield msg


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def transcript_summary(
    jsonl_path: Path,
    since: datetime | None = None,
) -> tuple[int, str | None, str | None]:
    """Compute (line_count, earliest_ts, latest_ts) for human display.

    Honors the same since-filter as iter_messages. Sorts timestamps by their
    parsed datetime value, not lexicographically, so a mixed-offset transcript
    (e.g., one event at +00:00 and another at -08:00) reports the chronological
    earliest/latest. Falls back to lex compare if parse fails.
    """
    count = 0
    earliest: str | None = None
    earliest_dt: datetime | None = None
    latest: str | None = None
    latest_dt: datetime | None = None
    for msg in iter_messages(jsonl_path, since=since):
        count += 1
        ts = msg.get("timestamp")
        if not isinstance(ts, str):
            continue
        dt = _parse_iso(ts)
        if dt is not None:
            if earliest_dt is None or dt < earliest_dt:
                earliest, earliest_dt = ts, dt
            if latest_dt is None or dt > latest_dt:
                latest, latest_dt = ts, dt
        else:
            if earliest is None or ts < earliest:
                earliest = ts
            if latest is None or ts > latest:
                latest = ts
    return count, earliest, latest
