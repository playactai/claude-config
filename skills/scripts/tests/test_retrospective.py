"""Tests for the retrospective skill helpers."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from skills.retrospective import proposals as P
from skills.retrospective import retrospect as R
from skills.retrospective import subagent as SUB
from skills.retrospective import transcript as T


@pytest.fixture(autouse=True)
def _permissive_projects_root(monkeypatch):
    """Most prompt-builder fixtures pass /tmp/* paths as project_dir. The
    real validate_project_dir refuses paths outside ~/.claude/projects/, so
    monkeypatch the projects root to / for the whole test module — the
    dedicated guard tests (test_validate_project_dir_*) re-monkeypatch back
    to a constrained root to exercise the rejection path."""
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", Path("/"))


def _retrospect_args(**overrides) -> argparse.Namespace:
    """Build an argparse.Namespace with the retrospect.py defaults."""
    base = {
        "step": 1,
        "cwd": None,
        "session_id": None,
        "since": "all",
        "transcript": None,
        "project_dir": None,
        "dry_run": False,
        "no_ledger": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)

# ============================================================================
# transcript.encode_cwd
# ============================================================================


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (Path("/Users/bill/.claude"), "-Users-bill--claude"),
        (Path("/Users/bill/git/myproject"), "-Users-bill-git-myproject"),
        (Path("/home/x/gitrepos/foo"), "-home-x-gitrepos-foo"),
        (Path("/a"), "-a"),
        (Path("/a/.b"), "-a--b"),
    ],
)
def test_encode_cwd(path: Path, expected: str):
    assert T.encode_cwd(path) == expected


def test_get_project_dir_uses_default_root(monkeypatch, tmp_path):
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path / "projects")
    pd = T.get_project_dir(Path("/home/x/foo"))
    assert pd == tmp_path / "projects" / "-home-x-foo"


def test_get_project_dir_custom_root(tmp_path):
    pd = T.get_project_dir(Path("/home/x/foo"), projects_root=tmp_path)
    assert pd == tmp_path / "-home-x-foo"


# ============================================================================
# transcript.find_active_project_dir
# ============================================================================


def test_find_active_project_dir_missing_root(tmp_path):
    assert T.find_active_project_dir(tmp_path / "nope") is None


def test_find_active_project_dir_empty_root(tmp_path):
    (tmp_path / "projects").mkdir()
    assert T.find_active_project_dir(tmp_path / "projects") is None


def test_find_active_project_dir_picks_latest_mtime(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    (root / "proj-a").mkdir()
    (root / "proj-b").mkdir()
    (root / "proj-a" / "old.jsonl").write_text("")
    time.sleep(0.01)
    (root / "proj-b" / "active.jsonl").write_text("")
    assert T.find_active_project_dir(root) == root / "proj-b"


# ============================================================================
# transcript.find_current_session
# ============================================================================


def test_find_current_session_missing_dir(tmp_path):
    assert T.find_current_session(tmp_path / "nope") is None


def test_find_current_session_empty_dir(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    assert T.find_current_session(project) is None


def test_find_current_session_picks_latest_mtime(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    older = project / "aaa.jsonl"
    newer = project / "bbb.jsonl"
    older.write_text("")
    time.sleep(0.01)
    newer.write_text("")
    assert T.find_current_session(project) == newer


def test_find_current_session_explicit_session_id(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "abc-123.jsonl"
    other = project / "def-456.jsonl"
    target.write_text("")
    other.write_text("")
    assert T.find_current_session(project, session_id="abc-123") == target


def test_find_current_session_explicit_id_missing(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    assert T.find_current_session(project, session_id="nope") is None


# ============================================================================
# transcript.parse_since
# ============================================================================


def test_parse_since_all():
    assert T.parse_since("all") is None
    assert T.parse_since("") is None


@pytest.mark.parametrize(
    ("spec", "delta"),
    [
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
        ("1w", timedelta(days=7)),
        ("1m", timedelta(days=30)),
        ("3h", timedelta(hours=3)),
    ],
)
def test_parse_since_relative(spec: str, delta: timedelta):
    now = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)
    assert T.parse_since(spec, now=now) == now - delta


def test_parse_since_iso_with_z():
    out = T.parse_since("2026-05-06T12:00:00Z")
    assert out == datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)


def test_parse_since_iso_with_offset():
    out = T.parse_since("2026-05-06T12:00:00+02:00")
    assert out is not None
    assert out.utcoffset() == timedelta(hours=2)


def test_parse_since_iso_naive_treated_as_utc():
    out = T.parse_since("2026-05-06T12:00:00")
    assert out == datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)


def test_parse_since_invalid_raises():
    with pytest.raises(ValueError):
        T.parse_since("not a date")


# ============================================================================
# transcript.iter_messages / transcript_summary
# ============================================================================


def _write_jsonl(path: Path, messages: list[dict]):
    with path.open("w", encoding="utf-8") as fh:
        for m in messages:
            fh.write(json.dumps(m) + "\n")


def test_iter_messages_skips_malformed(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        json.dumps({"type": "user", "timestamp": "2026-05-06T12:00:00Z"})
        + "\n"
        + "{not json"
        + "\n"
        + "\n"
        + json.dumps({"type": "assistant", "timestamp": "2026-05-06T12:01:00Z"})
        + "\n"
    )
    msgs = list(T.iter_messages(p))
    assert [m["type"] for m in msgs] == ["user", "assistant"]


def test_iter_messages_since_filter(tmp_path):
    p = tmp_path / "t.jsonl"
    _write_jsonl(
        p,
        [
            {"type": "user", "timestamp": "2026-05-06T08:00:00Z"},
            {"type": "user", "timestamp": "2026-05-06T12:00:00Z"},
            {"type": "user", "timestamp": "2026-05-06T16:00:00Z"},
        ],
    )
    cutoff = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    msgs = list(T.iter_messages(p, since=cutoff))
    assert len(msgs) == 2
    assert msgs[0]["timestamp"] == "2026-05-06T12:00:00Z"


def test_transcript_summary_counts_and_range(tmp_path):
    p = tmp_path / "t.jsonl"
    _write_jsonl(
        p,
        [
            {"type": "user", "timestamp": "2026-05-06T08:00:00Z"},
            {"type": "user", "timestamp": "2026-05-06T12:00:00Z"},
            {"type": "user", "timestamp": "2026-05-06T10:00:00Z"},
        ],
    )
    count, earliest, latest = T.transcript_summary(p)
    assert count == 3
    assert earliest == "2026-05-06T08:00:00Z"
    assert latest == "2026-05-06T12:00:00Z"


def test_transcript_summary_empty(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("")
    count, earliest, latest = T.transcript_summary(p)
    assert count == 0
    assert earliest is None
    assert latest is None


# ============================================================================
# proposals.normalize_rationale / fingerprint
# ============================================================================


def test_normalize_rationale_strips_punct_and_lowers():
    a = P.normalize_rationale("User SAID 'no, stop!'   ")
    b = P.normalize_rationale("user said no stop")
    assert a == b


def test_fingerprint_stable_across_runs():
    fp1 = P.fingerprint("/x", "memory:create", "User said no")
    fp2 = P.fingerprint("/x", "memory:create", "user said no")
    assert fp1 == fp2


def test_fingerprint_sensitive_to_target():
    fp1 = P.fingerprint("/x", "memory:create", "r")
    fp2 = P.fingerprint("/y", "memory:create", "r")
    assert fp1 != fp2


def test_fingerprint_sensitive_to_kind():
    fp1 = P.fingerprint("/x", "memory:create", "r")
    fp2 = P.fingerprint("/x", "memory:update", "r")
    assert fp1 != fp2


# ============================================================================
# proposals.proposal_from_dict / parse_proposals_payload
# ============================================================================


_GOOD_RAW = {
    "id": "p1",
    "target": "/abs/x.md",
    "kind": "memory:create",
    "rationale": "User said no twice",
    "name": "title",
    "description": "desc",
    "body": "body",
    "memory_type": "feedback",
    "evidence": ["line 1"],
    "severity": "medium",
}


def test_proposal_from_dict_happy():
    p = P.proposal_from_dict(_GOOD_RAW)
    assert p.id == "p1"
    assert p.evidence == ("line 1",)
    assert p.memory_type == "feedback"


@pytest.mark.parametrize("missing", ["id", "target", "kind", "rationale", "name", "description", "body"])
def test_proposal_from_dict_missing_required(missing: str):
    raw = dict(_GOOD_RAW)
    del raw[missing]
    with pytest.raises(ValueError, match=missing):
        P.proposal_from_dict(raw)


def test_proposal_from_dict_invalid_kind():
    raw = dict(_GOOD_RAW, kind="claude_md:create")
    with pytest.raises(ValueError, match="kind"):
        P.proposal_from_dict(raw)


def test_proposal_from_dict_invalid_memory_type():
    raw = dict(_GOOD_RAW, memory_type="anecdotal")
    with pytest.raises(ValueError, match="memory_type"):
        P.proposal_from_dict(raw)


def test_parse_proposals_payload_valid():
    payload = json.dumps({"version": 1, "session_id": "s", "proposals": [_GOOD_RAW]})
    out = P.parse_proposals_payload(payload)
    assert len(out) == 1
    assert out[0].id == "p1"


def test_parse_proposals_payload_empty_list():
    payload = json.dumps({"proposals": []})
    assert P.parse_proposals_payload(payload) == []


def test_parse_proposals_payload_malformed_json():
    """Malformed input must raise ValueError (exact message may vary as the
    fallback path goes through brace-balance check before json.loads)."""
    with pytest.raises(ValueError):
        P.parse_proposals_payload("{not json")


def test_parse_proposals_payload_missing_proposals_key():
    out = P.parse_proposals_payload(json.dumps({}))
    assert out == []


# ============================================================================
# proposals.dedupe
# ============================================================================


def test_dedupe_merges_evidence_and_keeps_first():
    a = P.proposal_from_dict(dict(_GOOD_RAW, id="a", evidence=["e1"]))
    b = P.proposal_from_dict(dict(_GOOD_RAW, id="b", evidence=["e2", "e1"]))
    out = P.dedupe([a, b])
    assert len(out) == 1
    assert out[0].id == "a"  # first occurrence wins
    assert out[0].evidence == ("e1", "e2")  # union, order preserved


def test_dedupe_preserves_distinct():
    a = P.proposal_from_dict(dict(_GOOD_RAW, id="a"))
    b = P.proposal_from_dict(dict(_GOOD_RAW, id="b", target="/other.md"))
    out = P.dedupe([a, b])
    assert {p.id for p in out} == {"a", "b"}


# ============================================================================
# proposals: ledger I/O
# ============================================================================


def test_read_ledger_missing(tmp_path):
    assert P.read_ledger(tmp_path / "nope.jsonl") == []


def test_append_and_read_ledger_roundtrip(tmp_path):
    ledger = tmp_path / "obs.jsonl"
    p = P.proposal_from_dict(_GOOD_RAW)
    e1 = P.make_ledger_entry(p, session_id="s1", status="deferred")
    e2 = P.make_ledger_entry(p, session_id="s2", status="applied")
    P.append_ledger(ledger, [e1])
    P.append_ledger(ledger, [e2])

    entries = P.read_ledger(ledger)
    assert [e.session for e in entries] == ["s1", "s2"]
    assert [e.status for e in entries] == ["deferred", "applied"]


def test_append_ledger_creates_parent_dirs(tmp_path):
    ledger = tmp_path / "nested" / "deep" / "obs.jsonl"
    assert not ledger.parent.exists()
    p = P.proposal_from_dict(_GOOD_RAW)
    P.append_ledger(ledger, [P.make_ledger_entry(p, session_id="s", status="applied")])
    assert ledger.exists()


def test_read_ledger_skips_malformed(tmp_path):
    ledger = tmp_path / "obs.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "ts": "2026-05-06T12:00:00Z",
                "session": "s1",
                "fingerprint": "fp",
                "target": "/x",
                "kind": "memory:create",
                "status": "seen",
                "signal_count": 1,
            }
        )
        + "\n"
        + "{not json\n"
        + "\n"
        + json.dumps({"missing": "fields"})
        + "\n"
    )
    entries = P.read_ledger(ledger)
    assert len(entries) == 1
    assert entries[0].session == "s1"


# ============================================================================
# proposals.tag_frequency
# ============================================================================


def test_tag_frequency_first_seen_is_new():
    p = P.proposal_from_dict(_GOOD_RAW)
    out = list(P.tag_frequency([p], ledger=[]))
    assert out == [(p, "NEW")]


def test_tag_frequency_recurring_when_prior_exists():
    p = P.proposal_from_dict(_GOOD_RAW)
    prior = P.make_ledger_entry(p, session_id="prior", status="deferred")
    out = list(P.tag_frequency([p], ledger=[prior]))
    assert out == [(p, "RECURRING")]


def test_tag_frequency_applied_overrides_later_deferred():
    p = P.proposal_from_dict(_GOOD_RAW)
    e_applied = P.make_ledger_entry(p, session_id="s1", status="applied")
    e_later = P.make_ledger_entry(p, session_id="s2", status="deferred")
    out = list(P.tag_frequency([p], ledger=[e_applied, e_later]))
    assert out == [(p, None)]


def test_tag_frequency_rejected_then_applied_suppresses():
    p = P.proposal_from_dict(_GOOD_RAW)
    e_rejected = P.make_ledger_entry(p, session_id="s1", status="rejected")
    e_applied = P.make_ledger_entry(p, session_id="s2", status="applied")
    out = list(P.tag_frequency([p], ledger=[e_rejected, e_applied]))
    assert out == [(p, None)]


def test_tag_frequency_suppressed_when_already_applied():
    p = P.proposal_from_dict(_GOOD_RAW)
    prior = P.make_ledger_entry(p, session_id="prior", status="applied")
    out = list(P.tag_frequency([p], ledger=[prior]))
    assert out == [(p, None)]


def test_tag_frequency_independent_proposals():
    a = P.proposal_from_dict(dict(_GOOD_RAW, id="a"))
    b = P.proposal_from_dict(dict(_GOOD_RAW, id="b", target="/other.md"))
    prior_a = P.make_ledger_entry(a, session_id="s", status="deferred")
    out = dict(P.tag_frequency([a, b], ledger=[prior_a]))
    assert out[a] == "RECURRING"
    assert out[b] == "NEW"


def test_make_ledger_entry_timestamp_format():
    p = P.proposal_from_dict(_GOOD_RAW)
    fixed = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)
    entry = P.make_ledger_entry(p, session_id="s", status="applied", now=fixed)
    assert entry.ts == "2026-05-06T12:00:00Z"


def test_make_ledger_entry_converts_aware_non_utc_to_utc():
    """Bug class: NYC noon != UTC noon; must convert before formatting."""
    from datetime import timezone as _tz

    p = P.proposal_from_dict(_GOOD_RAW)
    nyc = _tz(timedelta(hours=-4))
    nyc_noon = datetime(2026, 5, 6, 12, 0, 0, tzinfo=nyc)
    entry = P.make_ledger_entry(p, session_id="s", status="applied", now=nyc_noon)
    assert entry.ts == "2026-05-06T16:00:00Z"


def test_make_ledger_entry_naive_treated_as_utc():
    p = P.proposal_from_dict(_GOOD_RAW)
    naive = datetime(2026, 5, 6, 12, 0, 0)
    entry = P.make_ledger_entry(p, session_id="s", status="applied", now=naive)
    assert entry.ts == "2026-05-06T12:00:00Z"


# ============================================================================
# proposals: fence stripping in parse_proposals_payload
# ============================================================================


def test_parse_proposals_handles_fenced_json():
    body = json.dumps({"proposals": [_GOOD_RAW]})
    fenced = f"```json\n{body}\n```"
    out = P.parse_proposals_payload(fenced)
    assert len(out) == 1


def test_parse_proposals_handles_prose_wrapped_json():
    body = json.dumps({"proposals": [_GOOD_RAW]})
    wrapped = f"Here are my findings:\n{body}\nThat's all."
    out = P.parse_proposals_payload(wrapped)
    assert len(out) == 1


def test_parse_proposals_no_json_raises():
    with pytest.raises(ValueError, match="no balanced JSON"):
        P.parse_proposals_payload("just prose, no braces here")


def test_parse_proposals_unbalanced_braces_raises():
    """An unmatched `{` with no closing brace produces no balanced object."""
    with pytest.raises(ValueError, match="no balanced JSON"):
        P.parse_proposals_payload("prefix { broken")


# ============================================================================
# proposals.is_target_safe (path-traversal guard)
# ============================================================================


def test_is_target_safe_valid_target_under_memory(tmp_path):
    project_dir = tmp_path / "proj"
    (project_dir / "memory").mkdir(parents=True)
    target = project_dir / "memory" / "entry.md"
    assert P.is_target_safe(str(target), project_dir) is True


def test_is_target_safe_outside_memory_dir(tmp_path):
    project_dir = tmp_path / "proj"
    (project_dir / "memory").mkdir(parents=True)
    assert P.is_target_safe("/etc/passwd.md", project_dir) is False


def test_is_target_safe_path_traversal_rejected(tmp_path):
    project_dir = tmp_path / "proj"
    (project_dir / "memory").mkdir(parents=True)
    traversal = str(project_dir / "memory" / ".." / ".." / "etc.md")
    assert P.is_target_safe(traversal, project_dir) is False


def test_is_target_safe_relative_path_rejected(tmp_path):
    project_dir = tmp_path / "proj"
    (project_dir / "memory").mkdir(parents=True)
    assert P.is_target_safe("memory/x.md", project_dir) is False


# ============================================================================
# proposals: scope field
# ============================================================================


def test_proposal_default_scope_is_project_memory():
    p = P.proposal_from_dict(_GOOD_RAW)
    assert p.scope == "project_memory"


def test_proposal_explicit_unknown_scope_rejected():
    raw = dict(_GOOD_RAW, scope="claude_md")
    with pytest.raises(ValueError, match="scope"):
        P.proposal_from_dict(raw)


# ============================================================================
# proposals CLI: tag and append-ledger
# ============================================================================


def _run_proposals_cli(args: list[str], cwd=None) -> tuple[int, str, str]:
    """Run `python -m skills.retrospective.proposals ...` as a subprocess.

    Used to exercise the argparse / sys.exit boundary; pure helpers are
    tested directly above without going through the CLI.
    """
    import subprocess
    import sys as _sys

    result = subprocess.run(
        [_sys.executable, "-m", "skills.retrospective.proposals", *args],
        capture_output=True,
        text=True,
        cwd=cwd or Path(__file__).resolve().parent.parent,
        check=False,
    )
    return result.returncode, result.stdout, result.stderr


def test_cli_tag_emits_tagged_json(tmp_path):
    project_dir = tmp_path / "proj"
    memory_dir = project_dir / "memory"
    memory_dir.mkdir(parents=True)

    payload = tmp_path / "payload.json"
    target = memory_dir / "entry.md"
    payload.write_text(
        json.dumps(
            {
                "version": 1,
                "proposals": [
                    dict(_GOOD_RAW, id="p1", target=str(target)),
                ],
            }
        )
    )
    ledger = tmp_path / "obs.jsonl"
    output = tmp_path / "tagged.json"

    rc, _, err = _run_proposals_cli(
        [
            "tag",
            "--payload", str(payload),
            "--ledger", str(ledger),
            "--project-dir", str(project_dir),
            "--output", str(output),
        ]
    )
    assert rc == 0, err
    out = json.loads(output.read_text())
    assert out["count"] == 1
    assert out["proposals"][0]["frequency"] == "NEW"
    assert out["rejected_unsafe"] == []


def test_cli_tag_filters_unsafe_targets(tmp_path):
    project_dir = tmp_path / "proj"
    (project_dir / "memory").mkdir(parents=True)

    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps(
            {
                "version": 1,
                "proposals": [
                    dict(_GOOD_RAW, id="p1", target="/etc/passwd.md"),
                ],
            }
        )
    )
    ledger = tmp_path / "obs.jsonl"

    rc, stdout, err = _run_proposals_cli(
        [
            "tag",
            "--payload", str(payload),
            "--ledger", str(ledger),
            "--project-dir", str(project_dir),
        ]
    )
    assert rc == 0, err
    out = json.loads(stdout)
    assert out["count"] == 0
    assert len(out["rejected_unsafe"]) == 1


def test_cli_append_ledger_writes_entries(tmp_path):
    decisions = tmp_path / "dec.json"
    decisions.write_text(
        json.dumps(
            {
                "session_id": "s-1",
                "entries": [
                    {
                        "fingerprint": "abc",
                        "target": "/abs/x.md",
                        "kind": "memory:create",
                        "status": "applied",
                    },
                    {
                        "fingerprint": "def",
                        "target": "/abs/y.md",
                        "kind": "memory:update",
                        "status": "deferred",
                    },
                ],
            }
        )
    )
    ledger = tmp_path / "obs.jsonl"
    rc, _, err = _run_proposals_cli(
        ["append-ledger", "--decisions", str(decisions), "--ledger", str(ledger)]
    )
    assert rc == 0, err

    entries = P.read_ledger(ledger)
    assert len(entries) == 2
    assert entries[0].fingerprint == "abc"
    assert entries[0].status == "applied"
    assert entries[1].status == "deferred"


def test_cli_append_ledger_rejects_invalid_status(tmp_path):
    decisions = tmp_path / "dec.json"
    decisions.write_text(
        json.dumps(
            {
                "session_id": "s",
                "entries": [
                    {
                        "fingerprint": "abc",
                        "target": "/abs/x.md",
                        "kind": "memory:create",
                        "status": "promoted",  # not a valid status
                    },
                ],
            }
        )
    )
    ledger = tmp_path / "obs.jsonl"
    rc, _, err = _run_proposals_cli(
        ["append-ledger", "--decisions", str(decisions), "--ledger", str(ledger)]
    )
    assert rc != 0
    assert "status" in err.lower()
    assert not ledger.exists()  # nothing was written


# ============================================================================
# retrospect prompt builders: shell-safety + handoff + dry-run
# ============================================================================


def test_triage_prompt_shell_quotes_paths_with_spaces():
    """Reproduces the bug where projects under a path with a space split the
    generated tagger command into two args."""
    args = _retrospect_args(
        step=3,
        transcript="/tmp/My Project/x.jsonl",
        project_dir="/tmp/My Project",
        session_id="abc-123",
    )
    body = R.build_triage_body(args)
    assert "'/tmp/My Project'" in body
    assert "'/tmp/My Project/retrospective/observations.jsonl'" in body
    # Sanity: no unquoted bare paths in the actual command line.
    assert " --project-dir /tmp/My Project " not in body


def test_apply_prompt_shell_quotes_mkdir_and_helper_invocation():
    args = _retrospect_args(
        step=5,
        project_dir="/tmp/My Project",
        session_id="abc-123",
    )
    body = R.build_apply_body(args)
    assert "mkdir -p '/tmp/My Project/memory'" in body
    assert "--ledger '/tmp/My Project/retrospective/observations.jsonl'" in body


def test_apply_dry_run_prompt_omits_filesystem_mutations():
    """Dry-run contract: no Write/Edit/mkdir/rm/append-ledger instructions."""
    args = _retrospect_args(
        step=5,
        project_dir="/tmp/proj",
        session_id="abc",
        dry_run=True,
    )
    body = R.build_apply_body(args)

    assert "DRY RUN" in body
    # Negative assertions — the prompt MUST NOT tell the LLM to mutate state.
    forbidden = (
        "mkdir -p",
        "Use Write tool",
        "Use Edit tool",
        "Use Bash 'rm'",
        "append-ledger",
        # Placeholder tokens must not leak through unsubstituted.
        "__MKDIR_CMD__",
        "__APPEND_LEDGER_CMD__",
    )
    for phrase in forbidden:
        assert phrase not in body, f"dry-run prompt leaks live-write instruction: {phrase!r}"


def test_apply_live_prompt_substitutes_all_placeholders():
    """Live apply must not leak any __FOO__ placeholder tokens through."""
    args = _retrospect_args(
        step=5,
        project_dir="/tmp/proj",
        session_id="abc",
    )
    body = R.build_apply_body(args)
    # Every placeholder must be substituted.
    for token in (
        "__PROJECT_DIR__",
        "__INDEX_PATH__",
        "__LEDGER_PATH__",
        "__DECISIONS_PATH__",
        "__MKDIR_CMD__",
        "__MARK_COLLISIONS_TEMPLATE__",
        "__APPEND_LEDGER_CMD__",
        "__UPDATE_INDEX_TEMPLATE__",
        "__CLEANUP_CMD__",
    ):
        assert token not in body, f"live prompt leaks unresolved placeholder: {token}"


def test_triage_count_zero_branch_writes_empty_decisions():
    """When the tagger returns count==0, step 3 must materialize a valid empty
    decisions file before advancing. Otherwise step 5 reads a missing file."""
    args = _retrospect_args(step=3, project_dir="/tmp/proj", session_id="abc-123")
    body = R.build_triage_body(args)
    assert "If count == 0" in body
    assert "/tmp/retrospective-abc-123-decisions.json" in body
    assert '"applied": []' in body
    assert '"entries": []' in body
    assert '"session_id": "abc-123"' in body


def test_approve_empty_proposals_writes_empty_decisions():
    """Same fail-safe in step 4's empty-proposals fallback path."""
    args = _retrospect_args(step=4, session_id="abc-123")
    body = R.build_approve_body(args)
    assert "/tmp/retrospective-abc-123-decisions.json" in body
    assert '"applied": []' in body
    assert '"entries": []' in body
    assert '"session_id": "abc-123"' in body
    assert "--no-ledger" in body


def test_apply_prompt_checks_existence_before_memory_create():
    """memory:create must refuse to clobber an existing target. The prompt
    runs `test -e <item.target>` as a pre-flight probe BEFORE any writes,
    then flips colliding ids to deferred via the mark-collisions helper."""
    args = _retrospect_args(step=5, project_dir="/tmp/proj", session_id="abc")
    body = R.build_apply_body(args)
    assert "test -e <item.target>" in body
    # The pre-flight collects ids into COLLIDING_IDS; the helper does the flip.
    assert "COLLIDING_IDS" in body
    assert "mark-collisions" in body
    assert "<COMMA_SEPARATED_IDS>" in body
    assert "COLLISIONS:" in body
    # DEFERRED count is explicit arithmetic so the LLM does not double-count.
    assert "DEFERRED:   <U + C>" in body


def test_apply_prompt_orders_ledger_before_writes():
    """Ledger append happens BEFORE the per-item write loop so an interrupted
    run leaves a consistent ledger and re-runs suppress already-applied
    fingerprints. This pins the ordering."""
    args = _retrospect_args(step=5, project_dir="/tmp/proj", session_id="abc")
    body = R.build_apply_body(args)
    ledger_idx = body.find("append-ledger")
    write_loop_idx = body.find("RE-READ")
    assert 0 < ledger_idx < write_loop_idx, (
        "append-ledger must appear before the re-read/write-loop section"
    )


def test_apply_prompt_falls_back_to_write_on_missing_update_target():
    """memory:update against a non-existent file must fall back to Write
    (treat as create) instead of erroring out and stalling the loop."""
    args = _retrospect_args(step=5, project_dir="/tmp/proj", session_id="abc")
    body = R.build_apply_body(args)
    assert "Edit errors" in body
    assert "fall back to the Write tool" in body


def test_apply_prompt_cleans_up_tmp_files():
    """Per-session staging files in /tmp leak transcript-derived rationale.
    The live apply prompt must direct an `rm -f` cleanup at the end."""
    args = _retrospect_args(step=5, project_dir="/tmp/proj", session_id="abc-123")
    body = R.build_apply_body(args)
    assert "rm -f" in body
    assert "/tmp/retrospective-abc-123-payload.json" in body
    assert "/tmp/retrospective-abc-123-decisions.json" in body
    assert "/tmp/retrospective-abc-123-verdicts.json" in body


def test_apply_prompt_handles_mixed_collision_and_clean_create():
    """The collision branch is per-item, not all-or-nothing. The prompt must
    use 'for each' / per-item language so a single collision does not block
    sibling memory:create writes that have no existing target."""
    args = _retrospect_args(step=5, project_dir="/tmp/proj", session_id="abc")
    body = R.build_apply_body(args)
    assert "For each item in `applied`" in body


def test_apply_dry_run_prompt_previews_collisions():
    """Dry-run must surface collisions in its preview. The mutation contract
    is already covered by test_apply_dry_run_prompt_omits_filesystem_mutations
    so this only asserts the new preview behavior."""
    args = _retrospect_args(
        step=5,
        project_dir="/tmp/proj",
        session_id="abc",
        dry_run=True,
    )
    body = R.build_apply_body(args)
    assert "test -e <item.target>" in body
    assert "COLLISION" in body


def test_approve_prompt_caps_options_at_three():
    """AskUserQuestion's helper contract caps options at 2-3 per question.
    The prompt must not instruct the LLM to pass a fourth explicit option
    (apply/reject/defer/edit) or the call fails before the decisions file
    is written. The 'edit' sub-flow routes through the implicit 'Other'."""
    args = _retrospect_args(step=4, session_id="x")
    body = R.build_approve_body(args)
    assert "apply (Recommended)" in body
    assert "        - reject\n" in body
    assert "        - defer\n" in body
    # 'edit' must not appear as a fourth explicit option in the option list.
    assert "        - edit\n" not in body
    # The substantive-Other branch must be documented so the LLM knows
    # how to handle user-supplied edited bodies.
    assert "'Other'" in body


def test_approve_prompt_caps_batch_size_at_three():
    """AskUserQuestion accepts at most 3 questions per call in this project's
    helper contract — the prompt must direct batches of 3, not 4."""
    args = _retrospect_args(step=4, session_id="x")
    body = R.build_approve_body(args)
    assert "batches of 3" in body
    assert "batches of 4" not in body
    assert "1 to 3 proposals" in body


def test_empty_decisions_shape_consistent_across_fallbacks():
    """Step 3 (count==0) and step 4 (empty-proposals) both write the same
    decisions JSON shape that step 5's reader expects. If one drifts, step 5
    silently breaks. This test pins the shared shape across both branches."""
    triage = R.build_triage_body(
        _retrospect_args(step=3, project_dir="/tmp/p", session_id="x")
    )
    approve = R.build_approve_body(_retrospect_args(step=4, session_id="x"))
    expected = '{"session_id": "x", "applied": [], "entries": []}'
    assert expected in triage
    assert expected in approve


def test_no_step_prompt_references_undefined_cli_flags():
    """Earlier prompts told the LLM to invoke step 4 with --tagged-file and
    step 5 with --decisions-file, but main() never registered those flags.
    Following the printed handoff would crash argparse."""
    forbidden_flags = ("--tagged-file", "--decisions-file")
    args = _retrospect_args(
        step=3,
        transcript="/tmp/x.jsonl",
        project_dir="/tmp/proj",
        session_id="abc",
    )
    bodies = [
        R.build_triage_body(args),
        R.build_approve_body(_retrospect_args(step=4, session_id="abc")),
        R.build_apply_body(_retrospect_args(step=5, project_dir="/tmp/proj", session_id="abc")),
        R.build_apply_body(
            _retrospect_args(step=5, project_dir="/tmp/proj", session_id="abc", dry_run=True)
        ),
    ]
    for body in bodies:
        for flag in forbidden_flags:
            assert flag not in body, (
                f"prompt references {flag} but main() doesn't define it"
            )


def test_subagent_prompts_do_not_reference_undefined_transcript_flag():
    """subagent.py argparse only accepts --step. An earlier draft told the
    sub-agent --transcript was 'available when re-invoking', which would
    crash argparse. Pin that no subagent prompt mentions the flag."""
    for step in (1, 2, 3, 4):
        body = SUB.format_output(step)
        assert "--transcript" not in body, (
            f"subagent step {step} prompt references --transcript "
            "but the subagent argparse does not register it"
        )


def test_subagent_format_output_uses_static_steps():
    """subagent.py must dispatch via STATIC_STEPS dict (canon per
    codebase_analysis), not via WORKFLOW.steps lookup."""
    assert hasattr(SUB, "STATIC_STEPS")
    assert set(SUB.STATIC_STEPS.keys()) == {1, 2, 3, 4}
    # Each value is (title, instructions)
    for step, value in SUB.STATIC_STEPS.items():
        assert len(value) == 2
        title, instructions = value
        assert isinstance(title, str) and title
        assert isinstance(instructions, str) and instructions


def test_subagent_empty_transcript_emits_session_id():
    """The PARSE empty-transcript exit must emit a JSON shape with session_id
    so the parent's downstream consumers see a consistent envelope."""
    body = SUB.format_output(1)
    assert '"session_id":"<uuid>"' in body or '"session_id": "<uuid>"' in body
    assert '"proposals":[]' in body or '"proposals": []' in body


def test_retrospect_format_output_uses_dynamic_steps():
    """retrospect.py format_output dispatches via DYNAMIC_STEPS dict (canon)."""
    assert hasattr(R, "DYNAMIC_STEPS")
    assert set(R.DYNAMIC_STEPS.keys()) == {1, 2, 3, 4, 5}


# ============================================================================
# proposals: new CLI helpers (compose-decisions, mark-collisions, update-index)
# ============================================================================


def _make_tagged_fixture(path: Path, proposals: list[dict]) -> None:
    path.write_text(
        json.dumps({"version": 1, "session_id": "s", "count": len(proposals), "proposals": proposals}),
        encoding="utf-8",
    )


def test_cli_compose_decisions_carries_fingerprints_through(tmp_path):
    tagged = tmp_path / "tagged.json"
    _make_tagged_fixture(
        tagged,
        [
            {
                "id": "p1", "frequency": "NEW",
                "target": "/x/memory/a.md", "kind": "memory:create",
                "name": "A", "description": "a desc",
                "memory_type": "feedback", "body": "body A",
                "fingerprint": "fp_aaa",
                "rationale": "r", "evidence": [], "severity": "medium",
                "scope": "project_memory",
            },
            {
                "id": "p2", "frequency": "RECURRING",
                "target": "/x/memory/b.md", "kind": "memory:update",
                "name": "B", "description": "b desc",
                "memory_type": "project", "body": "body B",
                "fingerprint": "fp_bbb",
                "rationale": "r", "evidence": [], "severity": "medium",
                "scope": "project_memory",
            },
        ],
    )
    verdicts = tmp_path / "v.json"
    verdicts.write_text(
        json.dumps([{"id": "p1", "verdict": "apply"}, {"id": "p2", "verdict": "reject"}]),
        encoding="utf-8",
    )
    out = tmp_path / "decisions.json"
    rc, _, err = _run_proposals_cli(
        [
            "compose-decisions",
            "--tagged", str(tagged),
            "--verdicts", str(verdicts),
            "--session-id", "s",
            "--output", str(out),
        ]
    )
    assert rc == 0, err
    decisions = json.loads(out.read_text(encoding="utf-8"))
    assert decisions["session_id"] == "s"
    assert len(decisions["applied"]) == 1
    assert decisions["applied"][0]["id"] == "p1"
    assert decisions["applied"][0]["fingerprint"] == "fp_aaa"
    statuses = {e["fingerprint"]: e["status"] for e in decisions["entries"]}
    assert statuses == {"fp_aaa": "applied", "fp_bbb": "rejected"}


def test_cli_compose_decisions_handles_edited_body(tmp_path):
    tagged = tmp_path / "tagged.json"
    _make_tagged_fixture(
        tagged,
        [{
            "id": "p1", "frequency": "NEW",
            "target": "/x/memory/a.md", "kind": "memory:create",
            "name": "A", "description": "d", "memory_type": "feedback",
            "body": "ORIGINAL", "fingerprint": "fp",
            "rationale": "r", "evidence": [], "severity": "medium",
            "scope": "project_memory",
        }],
    )
    verdicts = tmp_path / "v.json"
    verdicts.write_text(
        json.dumps([{"id": "p1", "verdict": "apply", "edited_body": "EDITED"}]),
        encoding="utf-8",
    )
    out = tmp_path / "decisions.json"
    rc, _, err = _run_proposals_cli(
        [
            "compose-decisions",
            "--tagged", str(tagged),
            "--verdicts", str(verdicts),
            "--session-id", "s",
            "--output", str(out),
        ]
    )
    assert rc == 0, err
    decisions = json.loads(out.read_text(encoding="utf-8"))
    assert decisions["applied"][0]["body"] == "EDITED"


def test_cli_mark_collisions_flips_status_to_deferred(tmp_path):
    decisions = tmp_path / "d.json"
    decisions.write_text(
        json.dumps({
            "session_id": "s",
            "applied": [
                {"id": "p1", "target": "/x/a.md", "kind": "memory:create",
                 "name": "A", "description": "d", "memory_type": "feedback",
                 "body": "b", "fingerprint": "fp1"},
                {"id": "p2", "target": "/x/b.md", "kind": "memory:create",
                 "name": "B", "description": "d", "memory_type": "feedback",
                 "body": "b", "fingerprint": "fp2"},
            ],
            "entries": [
                {"fingerprint": "fp1", "target": "/x/a.md",
                 "kind": "memory:create", "status": "applied"},
                {"fingerprint": "fp2", "target": "/x/b.md",
                 "kind": "memory:create", "status": "applied"},
            ],
        }),
        encoding="utf-8",
    )
    rc, _, err = _run_proposals_cli(
        ["mark-collisions", "--decisions", str(decisions), "--collisions", "p1"]
    )
    assert rc == 0, err
    out = json.loads(decisions.read_text(encoding="utf-8"))
    assert [a["id"] for a in out["applied"]] == ["p2"]
    by_fp = {e["fingerprint"]: e["status"] for e in out["entries"]}
    assert by_fp == {"fp1": "deferred", "fp2": "applied"}


def test_cli_mark_collisions_rejects_unknown_id(tmp_path):
    decisions = tmp_path / "d.json"
    decisions.write_text(
        json.dumps({"session_id": "s", "applied": [], "entries": []}),
        encoding="utf-8",
    )
    rc, _, err = _run_proposals_cli(
        ["mark-collisions", "--decisions", str(decisions), "--collisions", "nope"]
    )
    assert rc != 0
    assert "unknown" in err.lower()


def test_cli_update_index_creates_missing_index(tmp_path):
    idx = tmp_path / "MEMORY.md"
    rc, _, err = _run_proposals_cli(
        [
            "update-index",
            "--index", str(idx),
            "--entry", "user_role|user_role.md|brief role memo",
        ]
    )
    assert rc == 0, err
    text = idx.read_text(encoding="utf-8")
    assert "- [user_role](user_role.md) — brief role memo" in text


def test_cli_update_index_is_idempotent(tmp_path):
    idx = tmp_path / "MEMORY.md"
    args = [
        "update-index",
        "--index", str(idx),
        "--entry", "feedback_x|feedback_x.md|first add",
    ]
    rc, _, _ = _run_proposals_cli(args)
    assert rc == 0
    rc, _, _ = _run_proposals_cli(args)
    assert rc == 0
    text = idx.read_text(encoding="utf-8")
    # Entry must appear exactly once.
    assert text.count("- [feedback_x](feedback_x.md) — first add") == 1


def test_cli_update_index_preserves_unrelated_lines(tmp_path):
    idx = tmp_path / "MEMORY.md"
    idx.write_text(
        "# Project memories\n"
        "\n"
        "- [unrelated](unrelated.md) — kept\n",
        encoding="utf-8",
    )
    rc, _, err = _run_proposals_cli(
        [
            "update-index",
            "--index", str(idx),
            "--entry", "new_one|new_one.md|added",
        ]
    )
    assert rc == 0, err
    text = idx.read_text(encoding="utf-8")
    assert "# Project memories" in text
    assert "- [unrelated](unrelated.md) — kept" in text
    assert "- [new_one](new_one.md) — added" in text


def test_cli_update_index_replaces_matching_basename(tmp_path):
    idx = tmp_path / "MEMORY.md"
    idx.write_text(
        "- [old_name](shared.md) — old description\n",
        encoding="utf-8",
    )
    rc, _, err = _run_proposals_cli(
        [
            "update-index",
            "--index", str(idx),
            "--entry", "new_name|shared.md|new description",
        ]
    )
    assert rc == 0, err
    text = idx.read_text(encoding="utf-8")
    assert "old_name" not in text
    assert "old description" not in text
    assert "- [new_name](shared.md) — new description" in text


def test_cli_tag_emits_clean_error_on_malformed_input(tmp_path):
    payload = tmp_path / "p.json"
    payload.write_text("this is not json at all", encoding="utf-8")
    ledger = tmp_path / "led.jsonl"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    rc, _, err = _run_proposals_cli(
        [
            "tag",
            "--payload", str(payload),
            "--ledger", str(ledger),
            "--project-dir", str(project_dir),
        ]
    )
    assert rc != 0
    # Should be a clean ERROR: line, not a full Python traceback.
    assert err.startswith("ERROR:") or "ERROR:" in err.splitlines()[-1]
    assert "Traceback" not in err


# ============================================================================
# proposals: JSON parser hardening
# ============================================================================


def test_extract_json_handles_stray_brace_before_payload():
    """Stray `{` in prose preceding a fenced JSON block must not trap the
    extractor at depth 1."""
    raw = "I noted { something earlier. Now: ```json\n{\"proposals\": []}\n```"
    out = P.parse_proposals_payload(raw)
    assert out == []


def test_extract_json_picks_last_fenced_block():
    """Multiple fenced JSON blocks: keep the last (LLM may draft and revise)."""
    raw = "```json\n{\"proposals\": [], \"version\": 0}\n```\nupdated:\n```json\n{\"proposals\": [], \"version\": 1}\n```"
    out = json.loads(P._extract_json_object(raw))
    assert out["version"] == 1


def test_extract_json_picks_last_balanced_object_in_prose():
    """When there's no fence, scan all balanced top-level objects and return
    the last (typically the actual payload, with prose preceding)."""
    raw = "Earlier context: {\"meta\": 1} . Final answer: {\"proposals\": [\"x\"]}"
    out = json.loads(P._extract_json_object(raw))
    assert out == {"proposals": ["x"]}


def test_proposal_from_dict_rejects_non_string_evidence():
    bad = dict(_GOOD_RAW, evidence=[{"line": 5, "text": "..."}])
    with pytest.raises(ValueError, match="evidence"):
        P.proposal_from_dict(bad)


def test_proposal_from_dict_accepts_string_evidence():
    good = dict(_GOOD_RAW, evidence=["line 5: 'x'", "line 6: 'y'"])
    p = P.proposal_from_dict(good)
    assert p.evidence == ("line 5: 'x'", "line 6: 'y'")


# ============================================================================
# transcript: chronological sort + deterministic mtime ties
# ============================================================================


def test_transcript_summary_sorts_by_parsed_datetime(tmp_path):
    """Mixed-offset ISO timestamps must order chronologically, not lex."""
    jsonl = tmp_path / "x.jsonl"
    jsonl.write_text(
        json.dumps({"timestamp": "2026-05-06T12:00:00-08:00"}) + "\n"  # 20:00 UTC
        + json.dumps({"timestamp": "2026-05-06T13:00:00+00:00"}) + "\n",  # 13:00 UTC
        encoding="utf-8",
    )
    count, earliest, latest = T.transcript_summary(jsonl)
    assert count == 2
    assert earliest is not None and latest is not None
    # +00:00 13:00 (13Z) is chronologically earlier than -08:00 12:00 (20Z).
    assert "13:00:00+00:00" in earliest
    assert "12:00:00-08:00" in latest


def test_find_active_project_dir_deterministic_on_tied_mtimes(tmp_path):
    """When two project dirs have identical mtimes, secondary path-string
    sort makes the choice deterministic."""
    root = tmp_path / "projects"
    root.mkdir()
    (root / "proj-a").mkdir()
    (root / "proj-b").mkdir()
    a = root / "proj-a" / "x.jsonl"
    b = root / "proj-b" / "x.jsonl"
    a.write_text("")
    b.write_text("")
    # Force exactly equal mtimes.
    import os
    fixed = 1700000000.0
    os.utime(a, (fixed, fixed))
    os.utime(b, (fixed, fixed))
    chosen = T.find_active_project_dir(root)
    assert chosen == root / "proj-a"  # path-string sort: -a before -b


# ============================================================================
# retrospect: --since friendly error + project_dir scope guard
# ============================================================================


def test_locate_body_reports_friendly_error_on_invalid_since(tmp_path, monkeypatch):
    """--since garbage must produce prose, not a Python traceback."""
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path)
    project_dir = tmp_path / "fake-encoded"
    project_dir.mkdir()
    (project_dir / "abc.jsonl").write_text("")
    args = _retrospect_args(step=1, project_dir=str(project_dir), since="garbage")
    body, next_cmd = R.build_locate_body(args)
    assert "--since value is invalid" in body
    assert "garbage" in body
    assert next_cmd is None  # terminate, don't advance


def test_apply_body_rejects_project_dir_outside_projects_root(monkeypatch, tmp_path):
    """The autouse fixture relaxes the projects root; this test re-pins it
    to a constrained dir so the validation guard fires for /etc."""
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path)
    args = _retrospect_args(step=5, project_dir="/etc", session_id="abc")
    body = R.build_apply_body(args)
    assert "is not under" in body
    assert "/etc" in body


def test_triage_body_rejects_project_dir_outside_projects_root(monkeypatch, tmp_path):
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path)
    args = _retrospect_args(step=3, project_dir="/etc", session_id="abc")
    body = R.build_triage_body(args)
    assert "is not under" in body


def test_forward_state_args_quotes_paths_with_spaces():
    args = _retrospect_args(
        transcript="/tmp/My Project/x.jsonl",
        project_dir="/tmp/My Project",
        session_id="abc",
    )
    out = R._forward_state_args(args)
    assert "--transcript '/tmp/My Project/x.jsonl'" in out
    assert "--project-dir '/tmp/My Project'" in out


def test_forward_state_args_emits_dry_run_and_no_ledger_flags():
    args = _retrospect_args(dry_run=True, no_ledger=True)
    out = R._forward_state_args(args)
    assert "--dry-run" in out
    assert "--no-ledger" in out


def test_cli_append_ledger_rejects_invalid_kind(tmp_path):
    decisions = tmp_path / "dec.json"
    decisions.write_text(
        json.dumps(
            {
                "session_id": "s",
                "entries": [
                    {
                        "fingerprint": "abc",
                        "target": "/abs/x.md",
                        "kind": "claude_md:create",  # v1 doesn't support this kind
                        "status": "applied",
                    },
                ],
            }
        )
    )
    ledger = tmp_path / "obs.jsonl"
    rc, _, err = _run_proposals_cli(
        ["append-ledger", "--decisions", str(decisions), "--ledger", str(ledger)]
    )
    assert rc != 0
    assert "kind" in err.lower()


# ============================================================================
# Edge cases and validation hardening
# ============================================================================


def test_extract_json_picks_valid_fence_when_earlier_fence_unbalanced():
    """If the first fenced block is unbalanced (LLM aborted draft) and a
    later fence has a valid object, the extractor must skip the bad fence
    rather than dragging across boundaries."""
    raw = (
        "```json\n"
        "{ unclosed\n"
        "```\n"
        "final answer:\n"
        "```json\n"
        '{"final": true}\n'
        "```\n"
    )
    out = json.loads(P._extract_json_object(raw))
    assert out == {"final": True}


def test_extract_json_string_aware_brace_count():
    """A `}` inside a JSON string must not close the wrapping object."""
    raw = 'prefix {"text": "value with } inside"} suffix'
    out = json.loads(P._extract_json_object(raw))
    assert out == {"text": "value with } inside"}


def test_compose_decisions_whitespace_edited_body_falls_through(tmp_path):
    """A whitespace-only edited_body is not a substantive edit; original
    body must be preserved."""
    tagged = tmp_path / "tagged.json"
    _make_tagged_fixture(
        tagged,
        [{
            "id": "p1", "frequency": "NEW",
            "target": "/x/memory/a.md", "kind": "memory:create",
            "name": "A", "description": "d", "memory_type": "feedback",
            "body": "ORIGINAL", "fingerprint": "fp",
            "rationale": "r", "evidence": [], "severity": "medium",
            "scope": "project_memory",
        }],
    )
    verdicts = tmp_path / "v.json"
    verdicts.write_text(
        json.dumps([{"id": "p1", "verdict": "apply", "edited_body": "   \n  "}]),
        encoding="utf-8",
    )
    out = tmp_path / "decisions.json"
    rc, _, err = _run_proposals_cli(
        [
            "compose-decisions",
            "--tagged", str(tagged),
            "--verdicts", str(verdicts),
            "--session-id", "s",
            "--output", str(out),
        ]
    )
    assert rc == 0, err
    decisions = json.loads(out.read_text(encoding="utf-8"))
    assert decisions["applied"][0]["body"] == "ORIGINAL"


def test_compose_decisions_rejects_non_dict_verdict(tmp_path):
    """A verdicts list element that isn't a JSON object must produce a clean
    ERROR, not a Python AttributeError traceback."""
    tagged = tmp_path / "tagged.json"
    _make_tagged_fixture(tagged, [])
    verdicts = tmp_path / "v.json"
    verdicts.write_text(json.dumps(["just a string"]), encoding="utf-8")
    out = tmp_path / "decisions.json"
    rc, _, err = _run_proposals_cli(
        [
            "compose-decisions",
            "--tagged", str(tagged),
            "--verdicts", str(verdicts),
            "--session-id", "s",
            "--output", str(out),
        ]
    )
    assert rc != 0
    assert err.startswith("ERROR:") or "ERROR:" in err.splitlines()[-1]
    assert "Traceback" not in err


def test_compose_decisions_rejects_duplicate_id(tmp_path):
    """Two entries with the same id pollute the ledger; reject loudly."""
    tagged = tmp_path / "tagged.json"
    _make_tagged_fixture(
        tagged,
        [{
            "id": "p1", "frequency": "NEW",
            "target": "/x/memory/a.md", "kind": "memory:create",
            "name": "A", "description": "d", "memory_type": "feedback",
            "body": "b", "fingerprint": "fp",
            "rationale": "r", "evidence": [], "severity": "medium",
            "scope": "project_memory",
        }],
    )
    verdicts = tmp_path / "v.json"
    verdicts.write_text(
        json.dumps([
            {"id": "p1", "verdict": "apply"},
            {"id": "p1", "verdict": "reject"},
        ]),
        encoding="utf-8",
    )
    out = tmp_path / "decisions.json"
    rc, _, err = _run_proposals_cli(
        [
            "compose-decisions",
            "--tagged", str(tagged),
            "--verdicts", str(verdicts),
            "--session-id", "s",
            "--output", str(out),
        ]
    )
    assert rc != 0
    assert "duplicate" in err.lower()


def test_compose_decisions_friendly_error_for_missing_id(tmp_path):
    tagged = tmp_path / "tagged.json"
    _make_tagged_fixture(tagged, [])
    verdicts = tmp_path / "v.json"
    verdicts.write_text(json.dumps([{"verdict": "apply"}]), encoding="utf-8")
    out = tmp_path / "decisions.json"
    rc, _, err = _run_proposals_cli(
        [
            "compose-decisions",
            "--tagged", str(tagged),
            "--verdicts", str(verdicts),
            "--session-id", "s",
            "--output", str(out),
        ]
    )
    assert rc != 0
    assert "missing 'id'" in err


def test_compose_decisions_friendly_error_for_malformed_tagged(tmp_path):
    """A tagged file lacking required fields surfaces id + missing-field, not
    bare KeyError."""
    tagged = tmp_path / "tagged.json"
    tagged.write_text(
        json.dumps({"version": 1, "session_id": "s", "proposals": [{"id": "p1"}]}),
        encoding="utf-8",
    )
    verdicts = tmp_path / "v.json"
    verdicts.write_text(json.dumps([{"id": "p1", "verdict": "apply"}]), encoding="utf-8")
    out = tmp_path / "decisions.json"
    rc, _, err = _run_proposals_cli(
        [
            "compose-decisions",
            "--tagged", str(tagged),
            "--verdicts", str(verdicts),
            "--session-id", "s",
            "--output", str(out),
        ]
    )
    assert rc != 0
    assert "p1" in err
    assert "fingerprint" in err


def test_mark_collisions_idempotent_on_retry(tmp_path):
    """Re-running mark-collisions with the same --collisions after a
    successful flip must not crash with 'unknown ids'."""
    decisions = tmp_path / "d.json"
    decisions.write_text(
        json.dumps({
            "session_id": "s",
            "applied": [
                {"id": "p1", "target": "/x/a.md", "kind": "memory:create",
                 "name": "A", "description": "d", "memory_type": "feedback",
                 "body": "b", "fingerprint": "fp1"},
            ],
            "entries": [
                {"fingerprint": "fp1", "target": "/x/a.md",
                 "kind": "memory:create", "status": "applied"},
            ],
        }),
        encoding="utf-8",
    )
    rc1, _, _ = _run_proposals_cli(
        ["mark-collisions", "--decisions", str(decisions), "--collisions", "p1"]
    )
    assert rc1 == 0
    # Retry with same id — must succeed (no-op), not error.
    rc2, _, err2 = _run_proposals_cli(
        ["mark-collisions", "--decisions", str(decisions), "--collisions", "p1"]
    )
    assert rc2 == 0, f"second invocation should be a no-op, got: {err2}"


def test_update_index_always_ends_with_newline(tmp_path):
    """File without trailing newline gets one; existing-with-newline keeps one."""
    idx = tmp_path / "MEMORY.md"
    idx.write_text("- [foo](foo.md) — orig", encoding="utf-8")
    rc, _, err = _run_proposals_cli(
        [
            "update-index",
            "--index", str(idx),
            "--entry", "foo|foo.md|new desc",
        ]
    )
    assert rc == 0, err
    text = idx.read_text(encoding="utf-8")
    assert text.endswith("\n")


def test_update_index_no_entries_is_noop(tmp_path):
    """Calling update-index with no --entry and a missing index must not
    create a 1-byte file."""
    idx = tmp_path / "MEMORY.md"
    rc, _, err = _run_proposals_cli(["update-index", "--index", str(idx)])
    assert rc == 0, err
    assert not idx.exists()


def test_parse_since_overflow_raises_value_error():
    """Huge --since values overflow timedelta; the helper must surface
    ValueError so build_locate_body's friendly path catches it."""
    with pytest.raises(ValueError, match="out of range"):
        T.parse_since("99999999999999999h")


def test_locate_body_handles_overflow_since(tmp_path, monkeypatch):
    """End-to-end: a huge --since must produce LOCATE_INVALID_SINCE prose,
    not an uncaught traceback."""
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path)
    project_dir = tmp_path / "encoded"
    project_dir.mkdir()
    (project_dir / "abc.jsonl").write_text("")
    args = _retrospect_args(step=1, project_dir=str(project_dir), since="99999999999999999h")
    body, next_cmd = R.build_locate_body(args)
    assert "--since value is invalid" in body
    assert next_cmd is None


def test_locate_body_rejects_project_dir_outside_projects_root(monkeypatch, tmp_path):
    """Step 1 itself must refuse --project-dir outside the projects root,
    not just steps 3 and 5."""
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path)
    args = _retrospect_args(step=1, project_dir="/etc")
    body, next_cmd = R.build_locate_body(args)
    assert "is not under" in body
    assert next_cmd is None


def test_dispatch_body_rejects_project_dir_outside_projects_root(monkeypatch, tmp_path):
    """Step 2 must also refuse — the sub-agent will be dispatched with the
    project_dir baked into its launching prompt, so an early refusal beats
    surfacing rejected_unsafe entries later."""
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path)
    args = _retrospect_args(step=2, project_dir="/etc", session_id="abc",
                            transcript="/tmp/x.jsonl")
    body = R.build_dispatch_body(args)
    assert "is not under" in body


def test_format_output_terminates_on_fatal_body_at_step_3(monkeypatch, tmp_path):
    """When triage emits a fatal-error body (e.g., bad project_dir), the
    NEXT STEP must be empty so the LLM does not advance into step 4 with
    the same forwarded bad state."""
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path)
    args = _retrospect_args(step=3, project_dir="/etc", session_id="abc")
    out = R.format_output(args)
    assert "is not under" in out
    # The format_step assembler emits an "execute this command now" line
    # only when next_cmd is non-empty — fatal bodies should NOT have one.
    assert "uv run python -m skills.retrospective.retrospect --step 4" not in out


def test_format_output_warns_on_placeholder_leak(monkeypatch, tmp_path):
    """Steps 2-5 invoked without step-1 state render placeholder-laced
    prompts. format_output must prepend a warning so the LLM doesn't
    follow the placeholder text in earnest."""
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path)
    # Step 3 uses project_dir; with project_dir=None it falls through to
    # the <PROJECT_DIR> placeholder.
    args = _retrospect_args(step=3, session_id="abc")
    out = R.format_output(args)
    assert "<PROJECT_DIR>" in out
    assert "WARNING" in out
    assert "Step 1 has not been run" in out


def test_subagent_parse_inlines_extract_branch_jq():
    """The PARSE prompt must inline the branch-extraction jq pattern, not
    point the sub-agent at another skill's SKILL.md (unstable across
    deployments and a soft cross-skill coupling)."""
    body = SUB.format_output(1)
    assert "skills/cc-history" not in body
    # The jq pattern must mention the key building blocks.
    assert "parentUuid" in body
    assert "leaves" in body or "leaf" in body


# ============================================================================
# Branched-leaf selection, memory:delete index cleanup, cross-project lookup
# ============================================================================


# ----- subagent: LEAF_SELECTION_JQ picks active branch by JSONL order -------


def _run_jq(jq_body: str, jsonl_text: str) -> list[dict]:
    """Execute the leaf-selection jq snippet against an in-memory JSONL.

    `jq` is required (the production sub-agent shells out to it for every
    invocation), so its absence is a fail-loud test failure — not a silent
    skip — to make sure the leaf-selection regression can't slip past CI.
    """
    import shutil
    import subprocess

    if shutil.which("jq") is None:
        pytest.fail(
            "jq binary not on PATH — install jq to run leaf-selection regression tests "
            "(the retrospective sub-agent requires jq at runtime)."
        )
    result = subprocess.run(
        ["jq", "-s", jq_body],
        input=jsonl_text,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_leaf_selection_jq_picks_most_recent_branch_not_lex_greatest_uuid():
    """Two-leaf transcript where the lex-greater UUID is the OLDER branch.

    The bug being fixed: ($leaves | sort | last) picks 'zzz-old-branch'
    by string sort, walking the abandoned fork. The fix selects by JSONL
    order, so the active branch (last leaf written) wins.
    """
    fixture = "\n".join([
        json.dumps({"uuid": "aaa-root", "parentUuid": None,
                    "timestamp": "2026-01-01T00:00:00Z", "type": "user"}),
        json.dumps({"uuid": "zzz-old-branch", "parentUuid": "aaa-root",
                    "timestamp": "2026-01-01T00:01:00Z", "type": "assistant"}),
        json.dumps({"uuid": "bbb-mid", "parentUuid": "aaa-root",
                    "timestamp": "2026-01-01T00:02:00Z", "type": "user"}),
        json.dumps({"uuid": "ccc-new-leaf", "parentUuid": "bbb-mid",
                    "timestamp": "2026-01-01T00:03:00Z", "type": "assistant"}),
    ])
    chain = _run_jq(SUB.LEAF_SELECTION_JQ, fixture)
    uuids = [m["uuid"] for m in chain]
    assert uuids == ["aaa-root", "bbb-mid", "ccc-new-leaf"]
    assert "zzz-old-branch" not in uuids


def test_leaf_selection_jq_handles_linear_transcript():
    """Single-branch transcript: the chain equals the entire input order."""
    fixture = "\n".join([
        json.dumps({"uuid": "u1", "parentUuid": None, "type": "user"}),
        json.dumps({"uuid": "u2", "parentUuid": "u1", "type": "assistant"}),
        json.dumps({"uuid": "u3", "parentUuid": "u2", "type": "user"}),
    ])
    chain = _run_jq(SUB.LEAF_SELECTION_JQ, fixture)
    assert [m["uuid"] for m in chain] == ["u1", "u2", "u3"]


def test_subagent_parse_prompt_inlines_leaf_selection_jq():
    """The exported jq constant must appear verbatim inside the PARSE prompt
    (drift between the two would silently re-introduce the bug)."""
    body = SUB.format_output(1)
    # Every non-blank line of the constant must be present in the rendered
    # prompt, modulo leading whitespace that `indent()` adds.
    for line in SUB.LEAF_SELECTION_JQ.splitlines():
        if line.strip():
            assert line.strip() in body, f"missing jq line in prompt: {line!r}"


# ----- proposals.cli_update_index: --delete -------------------------------


def test_cli_update_index_delete_removes_matching_basename(tmp_path):
    idx = tmp_path / "MEMORY.md"
    idx.write_text(
        "- [foo](foo.md) — keeper\n"
        "- [bar](bar.md) — to be removed\n",
        encoding="utf-8",
    )
    rc, _, err = _run_proposals_cli(
        ["update-index", "--index", str(idx), "--delete", "bar.md"]
    )
    assert rc == 0, err
    text = idx.read_text(encoding="utf-8")
    assert "bar.md" not in text
    assert "- [foo](foo.md) — keeper" in text
    assert text.endswith("\n")


def test_cli_update_index_delete_missing_basename_is_noop(tmp_path):
    idx = tmp_path / "MEMORY.md"
    original = "- [foo](foo.md) — only entry\n"
    idx.write_text(original, encoding="utf-8")
    rc, _, err = _run_proposals_cli(
        ["update-index", "--index", str(idx), "--delete", "nope.md"]
    )
    assert rc == 0, err
    assert idx.read_text(encoding="utf-8") == original


def test_cli_update_index_delete_combined_with_entry(tmp_path):
    idx = tmp_path / "MEMORY.md"
    idx.write_text(
        "- [old](old.md) — going away\n"
        "- [keep](keep.md) — preserved\n",
        encoding="utf-8",
    )
    rc, _, err = _run_proposals_cli(
        [
            "update-index",
            "--index", str(idx),
            "--entry", "added|added.md|new entry",
            "--delete", "old.md",
        ]
    )
    assert rc == 0, err
    text = idx.read_text(encoding="utf-8")
    assert "old.md" not in text
    assert "- [keep](keep.md) — preserved" in text
    assert "- [added](added.md) — new entry" in text


def test_cli_update_index_delete_only_no_entries(tmp_path):
    """File with one line + --delete of that basename → empty body, single
    trailing newline (POSIX-friendly)."""
    idx = tmp_path / "MEMORY.md"
    idx.write_text("- [solo](solo.md) — about to vanish\n", encoding="utf-8")
    rc, _, err = _run_proposals_cli(
        ["update-index", "--index", str(idx), "--delete", "solo.md"]
    )
    assert rc == 0, err
    assert idx.read_text(encoding="utf-8") == "\n"


def test_cli_update_index_delete_conflict_with_entry_errors(tmp_path):
    idx = tmp_path / "MEMORY.md"
    rc, _, err = _run_proposals_cli(
        [
            "update-index",
            "--index", str(idx),
            "--entry", "shared|shared.md|stays",
            "--delete", "shared.md",
        ]
    )
    assert rc != 0
    assert "shared.md" in err
    assert "both --entry and --delete" in err


def test_cli_update_index_print_reports_deleted_count(tmp_path):
    idx = tmp_path / "MEMORY.md"
    idx.write_text(
        "- [a](a.md) — first\n- [b](b.md) — second\n",
        encoding="utf-8",
    )
    rc, stdout, err = _run_proposals_cli(
        [
            "update-index",
            "--index", str(idx),
            "--entry", "c|c.md|third",
            "--delete", "a.md",
        ]
    )
    assert rc == 0, err
    payload = json.loads(stdout)
    assert payload["entries"] == 1
    assert payload["deleted"] == 1


def test_cli_update_index_noop_print_includes_zero_deleted(tmp_path):
    """The empty-input short-circuit still emits the canonical shape so
    downstream log scrapers don't need to special-case it."""
    idx = tmp_path / "MEMORY.md"
    rc, stdout, err = _run_proposals_cli(["update-index", "--index", str(idx)])
    assert rc == 0, err
    payload = json.loads(stdout)
    assert payload == {"index": str(idx), "entries": 0, "deleted": 0}


def test_build_update_index_template_surfaces_delete_flag():
    """The runtime template the LLM substitutes into must expose --delete so
    memory:delete proposals reach the helper."""
    template = R._build_update_index_template("/skills", "/idx/MEMORY.md")
    assert "--entry" in template
    assert "--delete" in template


# ----- transcript.find_session_across_projects ----------------------------


def test_find_session_across_projects_finds_in_other_project(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    (root / "proj-a").mkdir()
    (root / "proj-b").mkdir()
    (root / "proj-a" / "active.jsonl").write_text("")
    target = root / "proj-b" / "wanted-uuid.jsonl"
    target.write_text("")
    assert T.find_session_across_projects("wanted-uuid", root) == root / "proj-b"


def test_find_session_across_projects_returns_none_when_missing(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    (root / "proj-a").mkdir()
    (root / "proj-a" / "different.jsonl").write_text("")
    assert T.find_session_across_projects("missing", root) is None


def test_find_session_across_projects_returns_none_when_root_missing(tmp_path):
    assert T.find_session_across_projects("any", tmp_path / "nope") is None


def test_find_session_across_projects_rejects_glob_metacharacters(tmp_path):
    """A `--session-id "*"` (or `?`, `[abc]`) must NOT silently match every
    transcript on disk. The user pinned a specific UUID; matching arbitrary
    files would resolve to the wrong project under LOCATE_OK."""
    root = tmp_path / "projects"
    root.mkdir()
    (root / "proj-a").mkdir()
    (root / "proj-a" / "real-uuid.jsonl").write_text("")
    for malicious in ("*", "?", "[a-z]*", "*-uuid"):
        assert T.find_session_across_projects(malicious, root) is None, (
            f"glob metacharacter {malicious!r} matched a real file"
        )


def test_find_session_across_projects_rejects_path_separator(tmp_path):
    """A `--session-id "../proj-a/abc"` must NOT escape the projects root."""
    root = tmp_path / "projects"
    root.mkdir()
    (root / "proj-a").mkdir()
    (root / "proj-a" / "abc.jsonl").write_text("")
    assert T.find_session_across_projects("../proj-a/abc", root) is None
    assert T.find_session_across_projects("proj-a/abc", root) is None


def test_find_session_across_projects_picks_latest_on_collision(tmp_path):
    """If the same UUID lives under two project dirs (rare — manual move
    or symlink), the most-recently-modified copy wins."""
    root = tmp_path / "projects"
    root.mkdir()
    (root / "proj-a").mkdir()
    (root / "proj-b").mkdir()
    older = root / "proj-a" / "shared-uuid.jsonl"
    newer = root / "proj-b" / "shared-uuid.jsonl"
    older.write_text("")
    time.sleep(0.01)
    newer.write_text("")
    assert T.find_session_across_projects("shared-uuid", root) == root / "proj-b"


# ----- retrospect.resolve_project_dir: --session-id priority --------------


def test_resolve_project_dir_uses_session_lookup_when_only_session_id(monkeypatch, tmp_path):
    """No --cwd, no --project-dir, just --session-id. The hit's project
    must beat the most-recently-active autodetect path."""
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path)
    (tmp_path / "active-proj").mkdir()
    (tmp_path / "active-proj" / "other.jsonl").write_text("")
    time.sleep(0.01)
    (tmp_path / "wanted-proj").mkdir()
    target = tmp_path / "wanted-proj" / "uuid-123.jsonl"
    target.write_text("")
    # Touch active-proj's transcript afterward so it has the latest mtime
    # (would win autodetect if session-id didn't take precedence).
    (tmp_path / "active-proj" / "other.jsonl").write_text("touched")
    args = _retrospect_args(session_id="uuid-123")
    project_dir, source = R.resolve_project_dir(args)
    assert project_dir == tmp_path / "wanted-proj"
    assert source == "--session-id"


def test_resolve_project_dir_session_id_overrides_cwd_when_session_in_other_project(
    monkeypatch, tmp_path
):
    """Both --cwd and --session-id; UUID lives in a different project than
    --cwd. The UUID wins (more-specific signal)."""
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path)
    cwd_path = Path("/home/u/repo-a")
    cwd_dir = tmp_path / T.encode_cwd(cwd_path)
    cwd_dir.mkdir()
    (cwd_dir / "irrelevant.jsonl").write_text("")
    (tmp_path / "wanted-proj").mkdir()
    (tmp_path / "wanted-proj" / "uuid-xyz.jsonl").write_text("")
    args = _retrospect_args(cwd=str(cwd_path), session_id="uuid-xyz")
    project_dir, source = R.resolve_project_dir(args)
    assert project_dir == tmp_path / "wanted-proj"
    assert source == "--session-id"


def test_resolve_project_dir_session_lookup_falls_back_to_cwd_when_uuid_missing(
    monkeypatch, tmp_path
):
    """UUID nowhere on disk; --cwd is set; resolution falls back to cwd."""
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path)
    cwd_path = Path("/home/u/repo-x")
    cwd_dir = tmp_path / T.encode_cwd(cwd_path)
    cwd_dir.mkdir()
    args = _retrospect_args(cwd=str(cwd_path), session_id="ghost-uuid")
    project_dir, source = R.resolve_project_dir(args)
    assert project_dir == cwd_dir
    assert source == "--cwd"


def test_resolve_project_dir_session_lookup_falls_back_to_active_when_no_cwd(
    monkeypatch, tmp_path
):
    """UUID nowhere, no --cwd; falls back to find_active_project_dir."""
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    (tmp_path / "auto-active").mkdir()
    (tmp_path / "auto-active" / "x.jsonl").write_text("")
    args = _retrospect_args(session_id="ghost-uuid")
    project_dir, source = R.resolve_project_dir(args)
    assert project_dir == tmp_path / "auto-active"
    assert source == "auto-detect"


def test_locate_ok_body_reports_resolution_source(monkeypatch, tmp_path):
    """LOCATE_OK must show how the project was found so the user isn't
    confused by a project path they didn't pass on the CLI."""
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path)
    (tmp_path / "wanted").mkdir()
    (tmp_path / "wanted" / "session-abc.jsonl").write_text("")
    args = _retrospect_args(session_id="session-abc")
    body, next_cmd = R.build_locate_body(args)
    assert "Resolved via: --session-id" in body
    assert next_cmd is not None  # workflow advances to step 2


# ----- locate body: --cwd pointing at a project not yet on disk -----------


def test_locate_body_distinguishes_missing_cwd_from_empty_tree(monkeypatch, tmp_path):
    """When --cwd resolves to an encoded project dir that doesn't exist on
    disk, the message must name the encoded path and resolution source —
    not falsely claim the whole tree is empty (the original LOCATE_NO_TRANSCRIPTS
    wording was misleading for fresh projects when other projects had data)."""
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path)
    other = tmp_path / "-some-other-project"
    other.mkdir()
    (other / "abc.jsonl").write_text("")
    args = _retrospect_args(cwd="/home/x/fresh-project")
    body, next_cmd = R.build_locate_body(args)
    assert "Resolved project dir does not exist" in body
    assert "Resolved via: --cwd" in body
    assert "-home-x-fresh-project" in body
    assert next_cmd is None


def test_locate_body_no_transcripts_only_fires_on_empty_tree(monkeypatch, tmp_path):
    """LOCATE_NO_TRANSCRIPTS is reserved for an entirely empty projects root
    (no --cwd given, auto-detect returns None). The --cwd-missing case must
    take the LOCATE_NO_PROJECT_FOR_CWD branch instead."""
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path)
    args = _retrospect_args()
    body, next_cmd = R.build_locate_body(args)
    assert "No transcript files (*.jsonl) found under any project" in body
    assert next_cmd is None


# ----- dispatch invoke command embeds machine-readable --since/--transcript


def test_dispatch_invoke_cmd_embeds_since_and_transcript(monkeypatch, tmp_path):
    """The launching command for the sub-agent must carry --since and
    --transcript as argparse flags. The LLM follows the prose labels
    (TRANSCRIPT_PATH= / SINCE=), but the argparse contract pins them so a
    future refactor to subagent_dispatch / build_dispatch_body can't
    silently drop the window or make the sub-agent re-discover the path."""
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path)
    project_dir = tmp_path / "encoded"
    project_dir.mkdir()
    args = _retrospect_args(
        step=2,
        transcript=str(tmp_path / "x.jsonl"),
        project_dir=str(project_dir),
        since="24h",
    )
    body = R.build_dispatch_body(args)
    assert "--since 24h" in body
    assert "--transcript " in body
    assert str(tmp_path / "x.jsonl") in body


def test_dispatch_invoke_cmd_quotes_paths_with_spaces(monkeypatch, tmp_path):
    """shlex.quote must protect the launching command from breaking on
    transcripts whose paths contain whitespace."""
    monkeypatch.setattr(T, "DEFAULT_PROJECTS_ROOT", tmp_path)
    project_dir = tmp_path / "encoded"
    project_dir.mkdir()
    spaced = tmp_path / "My Project" / "x.jsonl"
    args = _retrospect_args(
        step=2,
        transcript=str(spaced),
        project_dir=str(project_dir),
        since="all",
    )
    body = R.build_dispatch_body(args)
    assert f"'{spaced}'" in body


def test_subagent_main_accepts_transcript_and_since(monkeypatch, capsys):
    """Sub-agent main() must round-trip the argparse flags the parent
    embeds in the launching command. The PARSE prompt itself reads from
    prose labels, but argparse accepting the flags is the contract that
    keeps a future refactor from silently dropping them."""
    monkeypatch.setattr(
        "sys.argv",
        [
            "subagent",
            "--step", "1",
            "--transcript", "/tmp/x.jsonl",
            "--since", "24h",
        ],
    )
    SUB.main()
    captured = capsys.readouterr()
    assert "PARSE" in captured.out
    assert "TRANSCRIPT_PATH=" in captured.out
