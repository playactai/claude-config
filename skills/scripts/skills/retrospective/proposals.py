"""Pure helpers for proposal data structures, deduplication, and the
observations ledger that drives [NEW]/[RECURRING] frequency tagging.

Also exposes a thin argparse CLI (`tag`, `append-ledger`) so the workflow
prompts in retrospect.py can delegate the mechanical bits — fingerprinting,
ledger I/O — to tested Python instead of restating them in prose.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

ProposalKind = Literal["memory:create", "memory:update", "memory:delete"]
ProposalScope = Literal["project_memory"]
MemoryType = Literal["user", "feedback", "project", "reference"]
ProposalStatus = Literal["applied", "rejected", "deferred"]
Frequency = Literal["NEW", "RECURRING"]

# Shared between step 3 (count==0 fallback) and step 4 (empty-proposals
# fallback) in retrospect.py. Single source of truth so the two paths can't
# drift out of step 5's reader expectations.
EMPTY_DECISIONS_JSON = '{"session_id": "<uuid>", "applied": [], "entries": []}'

_VERDICT_TO_STATUS = {"apply": "applied", "reject": "rejected", "defer": "deferred"}

_PUNCT = re.compile(r"[^\w\s]+")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Proposal:
    """A single proposed change. v1 only emits memory-targeting proposals.

    `scope` is reserved so v2 can extend to `claude_md`, `skill`, `rule`
    targets without breaking the on-disk JSON contract or ledger fingerprints.
    """

    id: str
    target: str
    kind: ProposalKind
    rationale: str
    name: str
    description: str
    body: str
    scope: ProposalScope = "project_memory"
    memory_type: MemoryType = "project"
    evidence: tuple[str, ...] = field(default_factory=tuple)
    severity: Literal["low", "medium", "high"] = "medium"

    def fingerprint(self) -> str:
        return fingerprint(self.target, self.kind, self.rationale)


def normalize_rationale(rationale: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Two rationales differing only in punctuation/case match.
    """
    s = rationale.lower()
    s = _PUNCT.sub(" ", s)
    s = _WHITESPACE.sub(" ", s).strip()
    return s


def fingerprint(target: str, kind: str, rationale: str) -> str:
    """Stable hash of (target, kind, normalized rationale)."""
    payload = f"{target}|{kind}|{normalize_rationale(rationale)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def proposal_from_dict(raw: dict) -> Proposal:
    """Construct a Proposal from sub-agent JSON, with light validation.

    Raises ValueError on missing or wrong-type required fields.
    """
    required = ("id", "target", "kind", "rationale", "name", "description", "body")
    missing = [k for k in required if k not in raw]
    if missing:
        raise ValueError(f"Proposal missing required fields: {missing}")

    kind = raw["kind"]
    if kind not in ("memory:create", "memory:update", "memory:delete"):
        raise ValueError(f"Unsupported proposal kind: {kind!r}")

    memory_type = raw.get("memory_type", "project")
    if memory_type not in ("user", "feedback", "project", "reference"):
        raise ValueError(f"Invalid memory_type: {memory_type!r}")

    severity = raw.get("severity", "medium")
    if severity not in ("low", "medium", "high"):
        raise ValueError(f"Invalid severity: {severity!r}")

    evidence = raw.get("evidence", [])
    if not isinstance(evidence, list):
        raise ValueError("evidence must be a list of strings")
    for i, e in enumerate(evidence):
        if not isinstance(e, str):
            raise ValueError(f"evidence[{i}] must be string, got {type(e).__name__}")

    scope = raw.get("scope", "project_memory")
    if scope != "project_memory":
        raise ValueError(f"Unsupported scope (v1 is project_memory only): {scope!r}")

    return Proposal(
        id=str(raw["id"]),
        target=str(raw["target"]),
        kind=kind,
        rationale=str(raw["rationale"]),
        name=str(raw["name"]),
        description=str(raw["description"]),
        body=str(raw["body"]),
        scope=scope,
        memory_type=memory_type,
        evidence=tuple(evidence),
        severity=severity,
    )


_FENCE_OPEN_RE = re.compile(r"```(?:json)?\s*", re.IGNORECASE)


def _scan_balanced_objects(text: str) -> list[str]:
    """Return all balanced top-level `{...}` substrings, in source order.

    Skips characters inside JSON strings so a `}` in `"foo} bar"` doesn't
    close the wrong brace. Walking is O(n).
    """
    out: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        end = -1
        for j in range(i, n):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end >= 0:
            out.append(text[i : end + 1])
            i = end + 1
        else:
            i += 1
    return out


def _extract_json_object(raw: str) -> str:
    """Extract a JSON object from raw text.

    Precedence:
      1. The whole input is `{...}` — return as-is.
      2. One or more fenced ```json ... ``` blocks — return the LAST one
         whose contents are JSON-parseable. (LLMs occasionally draft and
         revise; later fences are the final answer. Skipping unparseable
         fences avoids dragging across fence boundaries.)
      3. Scan for balanced top-level `{...}` substrings (string-aware);
         return the LAST. A stray `{` earlier in prose can't trap us
         because we retry from each `{` independently.

    Raises ValueError if no balanced object exists.
    """
    raw = raw.strip()
    if raw.startswith("{") and raw.endswith("}"):
        return raw

    # Walk fenced blocks: split on triple-backtick, scan balanced objects in
    # each fenced segment, validate each candidate parses, keep the last one
    # that parses cleanly. This is robust to an unbalanced first fence
    # interleaving with a valid later fence (regex non-greedy spans them).
    parts = raw.split("```")
    last_valid: str | None = None
    for idx, segment in enumerate(parts):
        if idx % 2 == 0:
            continue  # outside fences (prose)
        # Inside a fence: strip optional `json` language hint
        body = _FENCE_OPEN_RE.sub("", segment, count=1)
        for cand in _scan_balanced_objects(body):
            try:
                json.loads(cand)
            except json.JSONDecodeError:
                continue
            last_valid = cand
    if last_valid is not None:
        return last_valid

    # Bare-prose fallback: scan all balanced objects, return the last one
    # that parses. Validation prevents picking a malformed fragment over
    # a valid one further along.
    candidates = _scan_balanced_objects(raw)
    last_parsed: str | None = None
    for cand in candidates:
        try:
            json.loads(cand)
        except json.JSONDecodeError:
            continue
        last_parsed = cand
    if last_parsed is not None:
        return last_parsed
    if candidates:
        # Found balanced objects but none parsed — surface the parse error
        # rather than the structural one so callers see the real problem.
        json.loads(candidates[-1])  # raises JSONDecodeError
    raise ValueError("Sub-agent output contains no balanced JSON object")


def parse_proposals_payload(raw: str) -> list[Proposal]:
    """Parse a sub-agent JSON payload into a list of Proposals.

    Tolerates fenced code blocks and surrounding prose. Raises ValueError if
    no valid JSON object is found or the proposals field is malformed.
    """
    try:
        body = _extract_json_object(raw)
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError(f"Sub-agent output is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("Sub-agent payload must be a JSON object")
    items = data.get("proposals", [])
    if not isinstance(items, list):
        raise ValueError("payload['proposals'] must be a list")

    return [proposal_from_dict(item) for item in items]


def is_target_safe(target: str, project_dir: Path) -> bool:
    """Return True iff target resolves to a path under <project_dir>/memory/.

    Defense against prompt-injected proposals from the sub-agent: if a
    transcript message tricks the analysis sub-agent into emitting
    `target: "/etc/anything.md"`, the parent must refuse to surface it.
    """
    try:
        memory_root = (project_dir / "memory").resolve()
        resolved = Path(target).resolve()
    except (OSError, ValueError):
        return False
    return resolved.is_relative_to(memory_root)


def dedupe(proposals: Iterable[Proposal]) -> list[Proposal]:
    """Collapse proposals sharing a fingerprint, keeping the first occurrence.

    Evidence lists are merged across duplicates so no signal is lost.
    """
    seen: dict[str, Proposal] = {}
    order: list[str] = []
    for p in proposals:
        fp = p.fingerprint()
        if fp not in seen:
            seen[fp] = p
            order.append(fp)
            continue
        existing = seen[fp]
        merged_evidence = tuple(dict.fromkeys((*existing.evidence, *p.evidence)))
        seen[fp] = Proposal(
            id=existing.id,
            target=existing.target,
            kind=existing.kind,
            rationale=existing.rationale,
            name=existing.name,
            description=existing.description,
            body=existing.body,
            scope=existing.scope,
            memory_type=existing.memory_type,
            evidence=merged_evidence,
            severity=existing.severity,
        )
    return [seen[fp] for fp in order]


@dataclass(frozen=True)
class LedgerEntry:
    ts: str
    session: str
    fingerprint: str
    target: str
    kind: str
    status: ProposalStatus


def read_ledger(ledger_path: Path) -> list[LedgerEntry]:
    """Read all ledger lines. Missing file -> empty list.

    Malformed lines are skipped silently — a single bad write should never
    poison the whole ledger.
    """
    if not ledger_path.exists():
        return []
    out: list[LedgerEntry] = []
    with ledger_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                out.append(
                    LedgerEntry(
                        ts=str(obj["ts"]),
                        session=str(obj["session"]),
                        fingerprint=str(obj["fingerprint"]),
                        target=str(obj["target"]),
                        kind=str(obj["kind"]),
                        status=obj["status"],
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
    return out


def append_ledger(
    ledger_path: Path,
    entries: Iterable[LedgerEntry],
) -> None:
    """Append entries to the ledger, creating parents as needed."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(asdict(entry), separators=(",", ":")) + "\n")


def tag_frequency(
    proposals: Iterable[Proposal],
    ledger: Iterable[LedgerEntry],
) -> Iterator[tuple[Proposal, Frequency | None]]:
    """Yield (proposal, frequency) pairs.

    Frequency rules:
      - Any prior ledger entry with status='applied' for this fingerprint
        -> None (suppress: already applied, don't resurface).
      - Aggregate prior count == 0 -> 'NEW' (first time seen).
      - Aggregate prior count >= 1 -> 'RECURRING'.
    """
    counts: dict[str, int] = {}
    applied: set[str] = set()
    for entry in ledger:
        counts[entry.fingerprint] = counts.get(entry.fingerprint, 0) + 1
        if entry.status == "applied":
            applied.add(entry.fingerprint)

    for p in proposals:
        fp = p.fingerprint()
        if fp in applied:
            yield p, None
            continue
        prior = counts.get(fp, 0)
        yield p, ("RECURRING" if prior >= 1 else "NEW")


def _format_utc(now: datetime | None) -> str:
    """Render a UTC ISO timestamp regardless of the input's timezone.

    naïve datetimes are treated as UTC (matches parse_since); aware
    datetimes are converted to UTC so a non-UTC `now` doesn't write an
    incorrect 'Z'-suffixed string.
    """
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    else:
        now = now.astimezone(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_ledger_entry(
    proposal: Proposal,
    session_id: str,
    status: ProposalStatus,
    now: datetime | None = None,
) -> LedgerEntry:
    return LedgerEntry(
        ts=_format_utc(now),
        session=session_id,
        fingerprint=proposal.fingerprint(),
        target=proposal.target,
        kind=proposal.kind,
        status=status,
    )


# ============================================================================
# CLI ENTRY POINT
# ============================================================================
#
# Exposed so retrospect.py's step-3 / step-5 prompts can delegate the
# mechanical bits (fingerprint computation, dedupe, ledger I/O) to tested
# Python instead of restating them in prose. Drift between the two would
# poison the ledger across runs.


def _read_payload(path_or_dash: str) -> str:
    if path_or_dash == "-":
        return sys.stdin.read()
    return Path(path_or_dash).read_text(encoding="utf-8")


def _proposal_to_dict(p: Proposal) -> dict:
    return {
        "id": p.id,
        "target": p.target,
        "kind": p.kind,
        "rationale": p.rationale,
        "name": p.name,
        "description": p.description,
        "body": p.body,
        "scope": p.scope,
        "memory_type": p.memory_type,
        "evidence": list(p.evidence),
        "severity": p.severity,
        "fingerprint": p.fingerprint(),
    }


def cli_tag(args: argparse.Namespace) -> int:
    """`tag`: parse PROPOSALS payload, dedupe, tag NEW/RECURRING from ledger.

    Filters out proposals whose target escapes the project's memory dir,
    suppresses any whose fingerprint has a prior `applied` entry, and
    annotates the rest with `[NEW]` or `[RECURRING]`. Emits a JSON object
    to stdout (or --output) for the LLM to relay to the user in step 4.
    """
    try:
        raw = _read_payload(args.payload)
        try:
            envelope = json.loads(_extract_json_object(raw))
            session_id = str(envelope.get("session_id", ""))
        except (json.JSONDecodeError, ValueError):
            session_id = ""
        proposals = parse_proposals_payload(raw)
        proposals = dedupe(proposals)
        project_dir = Path(args.project_dir).expanduser()
        ledger = read_ledger(Path(args.ledger))

        safe: list[Proposal] = []
        rejected_unsafe: list[dict] = []
        for p in proposals:
            if is_target_safe(p.target, project_dir):
                safe.append(p)
            else:
                rejected_unsafe.append(
                    {"id": p.id, "target": p.target, "reason": "outside memory dir"}
                )

        tagged: list[dict] = []
        suppressed_applied = 0
        for p, freq in tag_frequency(safe, ledger):
            if freq is None:
                suppressed_applied += 1
                continue
            tagged.append({"frequency": freq, **_proposal_to_dict(p)})

        out = {
            "version": 1,
            "session_id": session_id,
            "ledger": str(args.ledger),
            "project_dir": str(project_dir),
            "count": len(tagged),
            "suppressed_already_applied": suppressed_applied,
            "rejected_unsafe": rejected_unsafe,
            "proposals": tagged,
        }
    except (ValueError, OSError) as e:
        sys.exit(f"ERROR: {e}")
    payload = json.dumps(out, indent=2)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


_REQUIRED_TAGGED_FIELDS = (
    "id", "fingerprint", "target", "kind",
    "name", "description", "memory_type", "body",
)


def cli_compose_decisions(args: argparse.Namespace) -> int:
    """`compose-decisions`: build the step-5 decisions JSON from tagged + verdicts.

    The LLM in step 4 collects `{id, verdict, edited_body?}` triples after
    AskUserQuestion and writes them to --verdicts. This helper reads the
    tagged file, joins by id, carries fingerprints through, applies any
    edited body, and emits the canonical decisions JSON. Doing the join
    in Python prevents the LLM from miscopying fingerprints or drifting
    the schema in prose.
    """
    try:
        tagged_data = json.loads(Path(args.tagged).read_text(encoding="utf-8"))
        verdicts_raw = json.loads(Path(args.verdicts).read_text(encoding="utf-8"))
        if not isinstance(verdicts_raw, list):
            sys.exit("ERROR: verdicts file must be a JSON list")

        # Validate tagged proposals up front so KeyError doesn't escape with
        # a bare key name; surface the offending id and missing field.
        by_id: dict[str, dict] = {}
        for tp in tagged_data.get("proposals", []):
            if not isinstance(tp, dict):
                sys.exit("ERROR: tagged proposals[] entry must be a JSON object")
            for f in _REQUIRED_TAGGED_FIELDS:
                if f not in tp:
                    sys.exit(
                        f"ERROR: tagged proposal {tp.get('id')!r} missing field {f!r}"
                    )
            by_id[tp["id"]] = tp

        applied: list[dict] = []
        entries: list[dict] = []
        seen_ids: set[str] = set()
        for i, v in enumerate(verdicts_raw):
            if not isinstance(v, dict):
                sys.exit(f"ERROR: verdicts[{i}] must be a JSON object")
            pid = v.get("id")
            verdict = v.get("verdict")
            if not pid:
                sys.exit(f"ERROR: verdicts[{i}] missing 'id'")
            if pid in seen_ids:
                sys.exit(f"ERROR: duplicate id in verdicts: {pid!r}")
            seen_ids.add(pid)
            if pid not in by_id:
                sys.exit(f"ERROR: unknown proposal id: {pid!r}")
            if verdict not in _VERDICT_TO_STATUS:
                sys.exit(
                    f"ERROR: verdict for {pid!r} must be one of "
                    f"{sorted(_VERDICT_TO_STATUS)}, got {verdict!r}"
                )
            p = by_id[pid]
            status = _VERDICT_TO_STATUS[verdict]
            entries.append(
                {
                    "fingerprint": p["fingerprint"],
                    "target": p["target"],
                    "kind": p["kind"],
                    "status": status,
                }
            )
            if verdict == "apply":
                # Treat None / missing / whitespace-only edited_body as
                # "no edit"; the user supplied no substantive replacement.
                edited = v.get("edited_body")
                use_edited = isinstance(edited, str) and edited.strip() != ""
                applied.append(
                    {
                        "id": pid,
                        "target": p["target"],
                        "kind": p["kind"],
                        "name": p["name"],
                        "description": p["description"],
                        "memory_type": p["memory_type"],
                        "body": edited if use_edited else p["body"],
                        "fingerprint": p["fingerprint"],
                    }
                )

        out = {
            "session_id": args.session_id,
            "applied": applied,
            "entries": entries,
        }
        Path(args.output).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"applied": len(applied), "entries": len(entries)}))
    except (ValueError, OSError, TypeError, AttributeError, json.JSONDecodeError) as e:
        sys.exit(f"ERROR: {e}")
    return 0


def cli_mark_collisions(args: argparse.Namespace) -> int:
    """`mark-collisions`: flip named applied entries to deferred status.

    Used by step 5 when a `memory:create` target already exists on disk and
    the user wasn't warned in step 4. Re-reads the decisions file in place,
    drops the named ids from `applied[]`, and rewrites their `entries[]`
    rows from `applied` -> `deferred`. The LLM then re-runs the apply pass
    only against the surviving applied entries.

    Idempotent: re-running with the same --collisions list after a
    successful flip is a no-op (ids already absent from applied[] are
    treated as already-flipped, not unknown).
    """
    try:
        decisions_path = Path(args.decisions)
        data = json.loads(decisions_path.read_text(encoding="utf-8"))
        ids = [s.strip() for s in args.collisions.split(",") if s.strip()]
        ids_set = set(ids)

        applied_ids = {a.get("id") for a in data.get("applied", [])}
        previously_flipped_ids = {
            e.get("id")
            for e in (data.get("_flipped_history", []))
        }
        known = applied_ids | previously_flipped_ids
        unknown = ids_set - known
        if unknown:
            sys.exit(f"ERROR: unknown ids: {sorted(unknown)}")

        flipped_fps = {
            a["fingerprint"] for a in data.get("applied", []) if a["id"] in ids_set
        }
        # Keep a tiny history so retries can validate against ids that were
        # previously-flipped (now absent from applied[]).
        history = data.setdefault("_flipped_history", [])
        for a in data.get("applied", []):
            if a["id"] in ids_set:
                history.append({"id": a["id"], "fingerprint": a["fingerprint"]})

        data["applied"] = [a for a in data.get("applied", []) if a["id"] not in ids_set]
        data["entries"] = [
            {**e, "status": "deferred"}
            if e.get("fingerprint") in flipped_fps and e.get("status") == "applied"
            else e
            for e in data.get("entries", [])
        ]

        decisions_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"flipped": len(flipped_fps), "remaining_applied": len(data["applied"])}))
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as e:
        sys.exit(f"ERROR: {e}")
    return 0


_INDEX_LINE_RE = re.compile(r"-\s*\[([^\]]+)\]\(([^)]+)\)")


def cli_update_index(args: argparse.Namespace) -> int:
    """`update-index`: idempotently maintain MEMORY.md index lines.

    Each --entry is `name|basename|description`. For each, ensure exactly
    one matching `- [name](basename) — description` line exists in the
    index. Existing matches (by basename) are updated in place; missing
    entries are appended. Unrelated lines are preserved as-is.

    Each --delete is a basename to remove (matched on the regex group 2 of
    `_INDEX_LINE_RE`). Repeatable. Missing basenames are no-ops.

    A basename appearing in both --entry and --delete is rejected loudly:
    the apply prompt should never produce that conflict, so silently
    picking a winner would mask a sub-agent bug.

    Output file always ends in a single `\\n`.
    """
    try:
        new_entries: list[tuple[str, str, str]] = []
        for raw in args.entry or []:
            parts = raw.split("|", 2)
            if len(parts) != 3:
                sys.exit(
                    f"ERROR: --entry must be 'name|basename|description', got {raw!r}"
                )
            new_entries.append(
                (parts[0].strip(), parts[1].strip(), parts[2].strip())
            )

        delete_basenames: set[str] = {
            bn.strip() for bn in (args.delete or []) if bn.strip()
        }

        new_by_basename = {bn: (name, bn, desc) for (name, bn, desc) in new_entries}
        conflicts = sorted(set(new_by_basename) & delete_basenames)
        if conflicts:
            sys.exit(
                f"ERROR: basename in both --entry and --delete: {', '.join(conflicts)}"
            )

        if not new_entries and not delete_basenames:
            # Nothing to write — don't touch the file.
            print(json.dumps({
                "index": str(args.index), "entries": 0, "deleted": 0,
            }))
            return 0

        index_path = Path(args.index)
        existing_text = (
            index_path.read_text(encoding="utf-8") if index_path.exists() else ""
        )
        existing_lines = existing_text.splitlines() if existing_text else []

        def canonical(name: str, basename: str, description: str) -> str:
            return f"- [{name}]({basename}) — {description}"

        out_lines: list[str] = []
        replaced: set[str] = set()
        deleted: set[str] = set()
        for line in existing_lines:
            m = _INDEX_LINE_RE.search(line)
            if m:
                bn = m.group(2).strip()
                if bn in delete_basenames:
                    deleted.add(bn)
                    continue
                if bn in new_by_basename:
                    if bn not in replaced:
                        out_lines.append(canonical(*new_by_basename[bn]))
                        replaced.add(bn)
                    continue
            out_lines.append(line)
        for (name, bn, desc) in new_entries:
            if bn not in replaced:
                out_lines.append(canonical(name, bn, desc))
                replaced.add(bn)

        index_path.parent.mkdir(parents=True, exist_ok=True)
        # Always end with a single trailing newline — POSIX-friendly and
        # what every downstream consumer (cat, tail, splitlines) expects.
        text = "\n".join(out_lines) + "\n"
        index_path.write_text(text, encoding="utf-8")
        print(json.dumps({
            "index": str(index_path),
            "entries": len(new_entries),
            "deleted": len(deleted),
        }))
    except (OSError, ValueError) as e:
        sys.exit(f"ERROR: {e}")
    return 0


def cli_append_ledger(args: argparse.Namespace) -> int:
    """`append-ledger`: append decision entries to the observations ledger.

    Input JSON shape (decisions field carries entries directly — no nested
    proposal envelopes — to keep the LLM's compose step simple):
        {"session_id": "<uuid>",
         "entries": [
            {"fingerprint": "<sha256>", "target": "<abs path>",
             "kind": "memory:create|update|delete",
             "status": "applied|rejected|deferred"},
            ...
         ]}

    Status is validated; unknown statuses fail loudly so a malformed prompt
    surfaces immediately instead of poisoning the ledger.
    """
    raw = _read_payload(args.decisions)
    data = json.loads(raw)
    if "session_id" not in data or "entries" not in data:
        sys.exit("ERROR: decisions JSON must have 'session_id' and 'entries' keys")

    valid_statuses = {"applied", "rejected", "deferred"}
    valid_kinds = {"memory:create", "memory:update", "memory:delete"}
    session_id = str(data["session_id"])
    entries: list[LedgerEntry] = []
    for d in data["entries"]:
        status = d.get("status")
        if status not in valid_statuses:
            sys.exit(f"ERROR: status must be one of {valid_statuses}, got {status!r}")
        kind = d.get("kind")
        if kind not in valid_kinds:
            sys.exit(f"ERROR: kind must be one of {valid_kinds}, got {kind!r}")
        for required in ("fingerprint", "target"):
            if not d.get(required):
                sys.exit(f"ERROR: entry missing required field {required!r}")
        entries.append(
            LedgerEntry(
                ts=_format_utc(None),
                session=session_id,
                fingerprint=str(d["fingerprint"]),
                target=str(d["target"]),
                kind=kind,
                status=status,
            )
        )

    append_ledger(Path(args.ledger), entries)
    print(json.dumps({"appended": len(entries), "ledger": str(args.ledger)}))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retrospective proposal helpers (CLI face for proposals.py).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    tag = sub.add_parser("tag", help="Dedupe + tag proposals from PROPOSALS JSON")
    tag.add_argument(
        "--payload", required=True,
        help="Path to PROPOSALS JSON file, or '-' for stdin",
    )
    tag.add_argument("--ledger", required=True, help="Path to observations.jsonl")
    tag.add_argument(
        "--project-dir", required=True,
        help="Project dir; proposals targeting paths outside <project-dir>/memory are rejected",
    )
    tag.add_argument("--output", default=None, help="Write tagged JSON here (default: stdout)")
    tag.set_defaults(func=cli_tag)

    append = sub.add_parser("append-ledger", help="Append decisions to the ledger")
    append.add_argument(
        "--decisions", required=True,
        help="Path to decisions JSON file, or '-' for stdin",
    )
    append.add_argument("--ledger", required=True, help="Path to observations.jsonl")
    append.set_defaults(func=cli_append_ledger)

    compose = sub.add_parser(
        "compose-decisions",
        help="Compose decisions JSON from tagged proposals + LLM verdicts",
    )
    compose.add_argument("--tagged", required=True, help="Tagged proposals JSON file")
    compose.add_argument(
        "--verdicts", required=True,
        help="Verdicts JSON list: [{id, verdict, edited_body?}, ...]",
    )
    compose.add_argument("--session-id", required=True, help="Session UUID")
    compose.add_argument("--output", required=True, help="Write decisions JSON here")
    compose.set_defaults(func=cli_compose_decisions)

    mark = sub.add_parser(
        "mark-collisions",
        help="Flip named applied entries in a decisions file to deferred",
    )
    mark.add_argument("--decisions", required=True, help="Decisions JSON file (in place)")
    mark.add_argument(
        "--collisions", required=True,
        help="Comma-separated proposal IDs to flip",
    )
    mark.set_defaults(func=cli_mark_collisions)

    idx = sub.add_parser("update-index", help="Idempotently update a MEMORY.md index")
    idx.add_argument("--index", required=True, help="Path to MEMORY.md")
    idx.add_argument(
        "--entry", action="append", default=[],
        help="'name|basename|description' (repeat for multiple entries)",
    )
    idx.add_argument(
        "--delete", action="append", default=[],
        help="Basename to remove from the index (repeatable; missing basenames are no-ops)",
    )
    idx.set_defaults(func=cli_update_index)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
