"""Tests for the planner batch-authoring round-trip fixes.

Covers:
- Fix 2: dispatch rejects unknown params (was a deep TypeError from func(ctx, **kwargs))
- Fix 3: create-required / version errors are RPC-neutral (no --flag names)
- Fix 4: batch JSON-decode errors carry a line/col + stdin hint (+ qr OSError parity)
- Fix 5: inline batch args are rejected with a guiding message (stdin only)
- Fix 1: step-6 prompt renders the exact RPC method catalog (underscore keys)
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from skills.planner.architect.plan_design_execute import (
    _render_method_catalog,
    get_step_guidance,
)
from skills.planner.cli import plan as plan_cli
from skills.planner.cli import plan_commands as pc
from skills.planner.cli import qr as qr_cli
from skills.planner.cli.dispatch import (
    _normalize_params,
    batch,
    discover_methods,
    dispatch,
    extract_params,
)


def _seed_plan(tmp_path: Path) -> pc.PlanContext:
    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    return ctx


# --- Fix 2: dispatch rejects unknown params --------------------------------


def test_dispatch_rejects_unknown_param():
    # Unknown keys are rejected before func is called, so ctx=None is fine.
    methods = discover_methods(pc)
    with pytest.raises(ValueError, match=r"Unknown params") as exc:
        dispatch(methods, "set-milestone", {"name": "A", "bogus": 1}, None)
    msg = str(exc.value)
    assert "bogus" in msg
    assert "name" in msg  # the valid set is listed for recovery


def test_dispatch_normalizes_hyphenated_param_key():
    # Fix 6: dispatch normalizes hyphenated param keys to underscores, matching
    # the method-name normalization already done by discover_methods.  The exact
    # mistake the catalog previously only warned about (decision-refs vs
    # decision_refs) is now accepted transparently.
    methods = discover_methods(pc)
    params = {"milestone": "M-001", "file": "a.py", "behavior": "b", "decision-refs": "DL-001"}
    # Normalization succeeds — the hyphenated key is canonicalized to decision_refs.
    normalized = _normalize_params("set-intent", params, *extract_params(methods["set-intent"]))
    assert "decision-refs" not in normalized
    assert "decision_refs" in normalized
    assert normalized["decision_refs"] == "DL-001"


def test_dispatch_still_rejects_truly_unknown_key():
    # Fix 6: normalization only rewrites hyphenated keys whose underscore form
    # is valid.  A genuinely unknown hyphenated key (no underscore equivalent)
    # is still rejected.
    methods = discover_methods(pc)
    params = {"milestone": "M-001", "file": "a.py", "behavior": "b", "bogus-key": "x"}
    with pytest.raises(ValueError, match=r"Unknown params"):
        dispatch(methods, "set-intent", params, None)


def test_batch_unknown_param_rolls_back_clean(tmp_path: Path):
    # Multi-op: a valid create followed by an unknown-param op -> the whole batch
    # rolls back atomically, the failing op carries a clean Unknown-params frame.
    ctx = _seed_plan(tmp_path)
    methods = discover_methods(pc)
    results = batch(
        methods,
        [
            {"method": "set-milestone", "params": {"name": "ok", "files": "a.py"}, "id": 1},
            {"method": "set-milestone", "params": {"name": "bad", "bogus": 1}, "id": 2},
        ],
        ctx,
    )
    assert results[0].get("rolled_back") is True  # the valid create was rolled back
    assert "error" in results[1]
    assert "Unknown params" in results[1]["error"]["message"]
    assert ctx.load_plan().milestones == []  # nothing persisted


# --- Fix 3: create/version errors are RPC-neutral (no --flag names) ---------


@pytest.mark.parametrize(
    ("method", "params", "needle"),
    [
        ("set-milestone", {}, "name required for create"),
        ("set-intent", {"milestone": "M-001"}, "file and behavior required for create"),
        ("set-decision", {}, "decision and reasoning required for create"),
        ("set-wave", {"milestones": ""}, "milestones required for create"),
    ],
)
def test_create_required_messages_have_no_dashdash(tmp_path: Path, method, params, needle):
    ctx = _seed_plan(tmp_path)
    pc.set_milestone(ctx, name="m0", files="a.py")  # M-001 exists (set-intent needs it)
    methods = discover_methods(pc)
    with pytest.raises(ValueError) as exc:
        dispatch(methods, method, params, ctx)
    msg = str(exc.value)
    assert needle in msg
    assert "--" not in msg


def test_version_mismatch_message_has_no_dashdash(tmp_path: Path):
    ctx = _seed_plan(tmp_path)
    pc.set_milestone(ctx, name="m0", files="a.py")  # M-001 at version 1
    methods = discover_methods(pc)
    with pytest.raises(ValueError, match=r"Version mismatch") as exc:
        dispatch(methods, "set-milestone", {"id": "M-001", "version": 99, "name": "x"}, ctx)
    assert "--" not in str(exc.value)


def test_relpath_validation_message_has_no_dashdash_on_rpc_path(tmp_path: Path):
    # set-milestone/set-intent are dispatchable; their path-validation errors must not
    # reference --files/--file (CLI flags) on the RPC path where keys are files/file.
    ctx = _seed_plan(tmp_path)
    methods = discover_methods(pc)
    with pytest.raises(ValueError, match=r"Absolute path") as exc:
        dispatch(methods, "set-milestone", {"name": "m", "files": "/etc/passwd"}, ctx)
    assert "--" not in str(exc.value)


def test_doc_only_intent_rejection_message_has_no_dashdash(tmp_path: Path):
    # set-intent on a documentation-only milestone is RPC-reachable; its remedy must
    # name the RPC field (documentation_only=false via set-milestone), not the CLI flag.
    ctx = _seed_plan(tmp_path)
    pc.set_milestone(ctx, name="docs", documentation_only=True)  # M-001, doc-only
    methods = discover_methods(pc)
    with pytest.raises(ValueError, match=r"documentation-only") as exc:
        dispatch(methods, "set-intent", {"milestone": "M-001", "file": "a.py", "behavior": "b"}, ctx)
    assert "--" not in str(exc.value)


# --- Fix 4: JSON-decode errors carry a location + stdin hint ----------------


def test_plan_cli_batch_invalid_json_message(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    with pytest.raises(SystemExit) as exc:
        plan_cli.cli(["--state-dir", str(tmp_path), "batch"])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Invalid JSON in batch input" in out
    assert "line 1 col" in out
    assert "batch &lt; changes.json" in out  # message is XML-escaped by error_exit


def test_qr_cli_batch_invalid_json_message(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    with pytest.raises(SystemExit):
        qr_cli.cli(["--state-dir", str(tmp_path), "--qr-phase", "impl-code", "batch"])
    out = capsys.readouterr().out
    assert "Invalid JSON in batch input" in out
    assert "line 1 col" in out


def test_plan_cli_batch_stdin_oserror_clean(tmp_path: Path, capsys, monkeypatch):
    # Symmetric to the qr OSError test: a stdin read failure surfaces as a clean
    # error frame (plan's generic clause already covers (ValueError, OSError)).
    class BoomStdin:
        def read(self, *args):
            raise OSError("stdin boom")

    monkeypatch.setattr(sys, "stdin", BoomStdin())
    with pytest.raises(SystemExit):
        plan_cli.cli(["--state-dir", str(tmp_path), "batch"])
    assert "<validation_error>" in capsys.readouterr().out


def test_qr_cli_batch_stdin_oserror_clean(tmp_path: Path, capsys, monkeypatch):
    # json.load(sys.stdin) -> stdin.read() raising OSError must surface as a clean
    # error frame, not a raw traceback (qr's except broadened to (ValueError, OSError)).
    class BoomStdin:
        def read(self, *args):
            raise OSError("stdin boom")

    monkeypatch.setattr(sys, "stdin", BoomStdin())
    with pytest.raises(SystemExit):
        qr_cli.cli(["--state-dir", str(tmp_path), "--qr-phase", "impl-code", "batch"])
    assert "<qr_cli_error>" in capsys.readouterr().out


# --- Fix 5: inline batch args are rejected (stdin only) ---------------------


def test_plan_cli_batch_inline_rejected(tmp_path: Path, capsys):
    inline = '[{"method":"list-decisions","params":{},"id":1}]'
    with pytest.raises(SystemExit) as exc:
        plan_cli.cli(["--state-dir", str(tmp_path), "batch", inline])
    assert exc.value.code == 1
    assert "reads JSON from stdin" in capsys.readouterr().out


def test_qr_cli_batch_inline_rejected(tmp_path: Path, capsys):
    with pytest.raises(SystemExit):
        qr_cli.cli(["--state-dir", str(tmp_path), "--qr-phase", "impl-code", "batch", "[]"])
    assert "reads JSON from stdin" in capsys.readouterr().out


# --- Fix 1: step-6 prompt renders the exact RPC method catalog --------------


def test_render_method_catalog_lists_exact_underscore_keys():
    body = "\n".join(_render_method_catalog())
    # exact underscore keys the architect previously inferred wrong
    for key in ("decision_refs", "node_id", "content_file", "documentation_only"):
        assert key in body
    for method in ("add-diagram-edge", "set-intent", "set-wave"):
        assert method in body


def test_step6_prompt_surfaces_catalog_and_notes():
    body = "\n".join(get_step_guidance(6)["actions"])
    assert "RPC METHOD CATALOG" in body
    assert "decision_refs" in body
    assert "underscores" in body  # hyphen-vs-underscore guidance
    assert "Unknown keys are rejected" in body
    assert "CREATE vs UPDATE" in body
    assert "version is rejected on create" in body
    # P2-A: set-wave update still needs milestones; set-intent infers its parent from the id
    assert "set-intent infers its parent milestone from" in body
    assert "set-wave still needs milestones" in body
    # P2-B: example is ordered (set-diagram before add-diagram-node)
    assert body.index('"method": "set-diagram"') < body.index('"method": "add-diagram-node"')
    # existing guards must still hold
    assert "set-diagram-render" in body
    assert "batch '[" not in body


def test_step6_example_batch_is_self_contained(tmp_path: Path):
    """The step-6 example batch must round-trip against a fresh skeleton.

    Proves both P2 fixes: the documented shapes (including set-intent's milestone
    on what would be an update) produce valid RPC, and set-diagram creates DIAG-001
    before add-diagram-node/edge reference it.
    """
    ctx = _seed_plan(tmp_path)
    methods = discover_methods(pc)
    results = batch(
        methods,
        [
            {"method": "set-decision", "params": {"decision": "Use polling", "reasoning": "30% webhook failures"}, "id": 1},
            {"method": "set-milestone", "params": {"name": "Auth stack", "files": "src/auth.py"}, "id": 2},
            {"method": "set-intent", "params": {"milestone": "M-001", "file": "src/auth.py", "behavior": "Add token validation", "decision_refs": "DL-001"}, "id": 3},
            {"method": "set-wave", "params": {"milestones": "M-001"}, "id": 4},
            {"method": "set-diagram", "params": {"type": "architecture", "scope": "overview", "title": "System Overview"}, "id": 5},
            {"method": "add-diagram-node", "params": {"diagram": "DIAG-001", "node_id": "client", "label": "Client", "type": "service"}, "id": 6},
            {"method": "add-diagram-node", "params": {"diagram": "DIAG-001", "node_id": "server", "label": "Server", "type": "service"}, "id": 7},
            {"method": "add-diagram-edge", "params": {"diagram": "DIAG-001", "source": "client", "target": "server", "label": "calls", "protocol": "gRPC"}, "id": 8},
        ],
        ctx,
    )
    # Every op must succeed — no errors.
    for r in results:
        assert "result" in r, f"unexpected error in batch result: {r}"
    # validate --phase plan-design must also pass.
    pc.validate(ctx, "plan-design")


# --- Fix G (round-2): normalized params, CSV isolation, strict version -------
#
# Each test below exercises dispatch/batch (not set_intent directly) so
# _normalize_params and its CSV-aware key+value-shape rules are exercised by
# the real production path.  Reverting any production hunk must fail a test.


# N1: ambiguous hyphenated + underscore forms are rejected (both key orders)
@pytest.mark.parametrize(
    "params",
    [
        {"milestone": "M-001", "decision_refs": "DL-001", "decision-refs": "DL-002"},
        {"milestone": "M-001", "decision-refs": "DL-002", "decision_refs": "DL-001"},
    ],
)
def test_ambiguous_hyphen_and_underscore_rejected(tmp_path: Path, params):
    ctx = _seed_plan(tmp_path)
    pc.set_decision(ctx, decision="d", reasoning="r")  # DL-001
    pc.set_milestone(ctx, name="m0")  # M-001
    methods = discover_methods(pc)
    with pytest.raises(ValueError, match=r"Ambiguous") as exc:
        dispatch(methods, "set-intent", params, ctx)
    assert "decision_refs" in str(exc.value)


# S4 + CSV drift guard: every CSV_PARAM_NAMES param round-trips a single-element
# array with an internal comma as ONE element (not comma-split).  decision_refs
# and milestones are verified separately because their validation requires
# resolved references.
@pytest.mark.parametrize(
    ("csv_param", "create_op"),
    [
        ("files", {"name": "ms", "files": ["a.py,b.py"]}),
        ("flags", {"name": "ms", "flags": ["-x,-y"]}),
        ("requirements", {"name": "ms", "requirements": ["req1,req2"]}),
        ("acceptance_criteria", {"name": "ms", "acceptance_criteria": ["ac1,ac2"]}),
        ("tests", {"name": "ms", "tests": ["t1,t2"]}),
    ],
)
def test_csv_param_internal_comma_not_split(tmp_path: Path, csv_param, create_op):
    """A single-element array with an internal comma must stay as one entry.

    Fix S4: without the CSV_PARAM_NAMES guard, _normalize_params unwraps the
    single-element list, then parse_csv comma-splits the unwrapped string --
    corrupting the value. With the guard, the list passes through untouched.
    """
    ctx = _seed_plan(tmp_path)
    methods = discover_methods(pc)
    dispatch(methods, "set-milestone", create_op, ctx)
    plan = json.loads(ctx.plan_path().read_text(encoding="utf-8"))
    actual = plan["milestones"][0][csv_param]
    assert isinstance(actual, list), f"expected list, got {type(actual).__name__}: {actual}"
    assert len(actual) == 1, f"expected 1 element, got {len(actual)}: {actual}"
    assert "," in actual[0], f"internal comma lost: {actual[0]!r}"


def test_decision_refs_list_input_roundtrip(tmp_path: Path):
    """decision_refs as a JSON array passes through _normalize_params untouched."""
    ctx = _seed_plan(tmp_path)
    pc.set_decision(ctx, decision="d", reasoning="r")  # DL-001
    pc.set_milestone(ctx, name="ms")  # M-001
    methods = discover_methods(pc)
    dispatch(methods, "set-intent", {
        "milestone": "M-001", "file": "a.py", "behavior": "b", "decision_refs": ["DL-001"],
    }, ctx)
    plan = json.loads(ctx.plan_path().read_text(encoding="utf-8"))
    assert plan["milestones"][0]["code_intents"][0]["decision_refs"] == ["DL-001"]


def test_milestones_list_input_roundtrip(tmp_path: Path):
    """milestones as a JSON array passes through _normalize_params untouched."""
    ctx = _seed_plan(tmp_path)
    pc.set_milestone(ctx, name="ms", files="a.py")  # M-001
    methods = discover_methods(pc)
    dispatch(methods, "set-wave", {"milestones": ["M-001"]}, ctx)
    plan = json.loads(ctx.plan_path().read_text(encoding="utf-8"))
    assert plan["waves"][0]["milestones"] == ["M-001"]


# S3: non-string list elements in CSV params raise a clean error
@pytest.mark.parametrize(
    ("bad_value",),
    [
        pytest.param([123], id="bare-int"),
        pytest.param(["a.py", None], id="mixed-with-null"),
    ],
)
def test_csv_param_rejects_non_string_elements(tmp_path: Path, bad_value):
    ctx = _seed_plan(tmp_path)
    methods = discover_methods(pc)
    with pytest.raises(ValueError, match=r"expected a list of strings") as exc:
        dispatch(methods, "set-milestone", {"name": "ms", "files": bad_value}, ctx)
    msg = str(exc.value)
    assert ".split" not in msg
    assert ".keys" not in msg
    assert "NoneType" not in msg


# bool/E1/N2: version type validation — strict
@pytest.mark.parametrize(
    ("version", "expected_outcome"),
    [
        pytest.param(True, "error", id="bool-true"),
        pytest.param(1.5, "error", id="float-non-whole"),
        pytest.param(1.0, "error", id="float-whole"),
        pytest.param("abc", "error", id="str-non-numeric"),
        pytest.param("1", "ok", id="str-numeric"),
        pytest.param(1, "ok", id="int"),
    ],
)
def test_version_strict_typing(tmp_path: Path, version, expected_outcome):
    ctx = _seed_plan(tmp_path)
    pc.set_milestone(ctx, name="ms", files="a.py")  # M-001 at version 1
    methods = discover_methods(pc)
    try:
        dispatch(methods, "set-milestone", {"id": "M-001", "version": version, "name": "x"}, ctx)
        outcome = "ok"
    except ValueError:
        outcome = "error"
    assert outcome == expected_outcome, (
        f"expected {expected_outcome} for version={version!r}, got {outcome}"
    )


def test_version_strict_readback_not_updated_on_failure(tmp_path: Path):
    """Entity version must NOT be updated when a bad version value is rejected."""
    ctx = _seed_plan(tmp_path)
    pc.set_milestone(ctx, name="ms", files="a.py")  # M-001, version 1
    methods = discover_methods(pc)
    for bad in (True, 1.0, 1.5):
        with pytest.raises(ValueError):
            dispatch(methods, "set-milestone", {"id": "M-001", "version": bad, "name": "x"}, ctx)
    plan_json = json.loads(ctx.plan_path().read_text(encoding="utf-8"))
    assert plan_json["milestones"][0]["version"] == 1


# null (S1/S7): params:null must produce a clean method-level frame, not
# "'NoneType' object has no attribute 'keys'"
def test_batch_null_params_clean_frame(tmp_path: Path):
    ctx = _seed_plan(tmp_path)
    methods = discover_methods(pc)
    results = batch(methods, [{"method": "list-milestones", "params": None, "id": 1}], ctx)
    assert "error" not in results[0], f"unexpected error: {results[0]}"
    assert "result" in results[0]


# nested/E4: [[ "M-001" ]] unwraps recursively; ["M-001", "M-002"] rejected
def test_scalar_nested_list_unwrap_succeeds(tmp_path: Path):
    ctx = _seed_plan(tmp_path)
    pc.set_milestone(ctx, name="ms")  # M-001
    methods = discover_methods(pc)
    result = dispatch(methods, "set-intent", {
        "milestone": [["M-001"]], "file": "a.py", "behavior": "b",
    }, ctx)
    assert result["id"] == "CI-M-001-001"


def test_scalar_multi_element_list_rejected(tmp_path: Path):
    ctx = _seed_plan(tmp_path)
    pc.set_milestone(ctx, name="ms")  # M-001
    methods = discover_methods(pc)
    with pytest.raises(ValueError, match=r"must be a single value") as exc:
        dispatch(methods, "set-intent", {
            "milestone": ["M-001", "M-002"], "file": "a.py", "behavior": "b",
        }, ctx)
    msg = str(exc.value)
    assert "must be a single value" in msg
    # The old "Milestone ['M-001'] not found" message must be gone
    assert "not found" not in msg


# N3: batch multi-element scalar params get clean "must be a single value"
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("set-milestone", {"name": ["A", "B"]}),
        ("set-decision", {"decision": ["a", "b"], "reasoning": "r"}),
    ],
)
def test_batch_scalar_list_clean_rejection(tmp_path: Path, method, params):
    ctx = _seed_plan(tmp_path)
    methods = discover_methods(pc)
    results = batch(methods, [{"method": method, "params": params, "id": 1}], ctx)
    assert "error" in results[0]
    msg = results[0]["error"]["message"]
    assert "must be a single value" in msg, f"wrong message: {msg}"
    # No pydantic or internal type leak
    assert "string_type" not in msg


# D1 post-state: decision_refs created, then cleared via [] / omission
def test_decision_refs_clear_roundtrip(tmp_path: Path):
    """Create intent with decision_refs, then clear via [] on update (read-back = [])."""
    ctx = _seed_plan(tmp_path)
    pc.set_decision(ctx, decision="d", reasoning="r")  # DL-001
    pc.set_milestone(ctx, name="ms")  # M-001
    methods = discover_methods(pc)

    dispatch(methods, "set-intent", {
        "milestone": "M-001", "file": "a.py", "behavior": "b", "decision_refs": "DL-001",
    }, ctx)

    # Update with empty array: clears decision_refs
    dispatch(methods, "set-intent", {
        "id": "CI-M-001-001", "version": 1, "decision_refs": [], "behavior": "b",
    }, ctx)
    plan_json = json.loads(ctx.plan_path().read_text(encoding="utf-8"))
    assert plan_json["milestones"][0]["code_intents"][0].get("decision_refs") == []


def test_decision_refs_preserved_on_omit(tmp_path: Path):
    """Update intent without passing decision_refs -> existing refs preserved."""
    ctx = _seed_plan(tmp_path)
    pc.set_decision(ctx, decision="d", reasoning="r")  # DL-001
    pc.set_milestone(ctx, name="ms")  # M-001
    methods = discover_methods(pc)

    dispatch(methods, "set-intent", {
        "milestone": "M-001", "file": "a.py", "behavior": "init", "decision_refs": "DL-001",
    }, ctx)

    # Update omitting decision_refs entirely
    dispatch(methods, "set-intent", {
        "id": "CI-M-001-001", "version": 1, "behavior": "updated",
    }, ctx)
    plan_json = json.loads(ctx.plan_path().read_text(encoding="utf-8"))
    assert plan_json["milestones"][0]["code_intents"][0].get("decision_refs") == ["DL-001"]


# C2: qr malformed-JSON batch hints at --qr-phase (not plan's usage)
def test_qr_batch_invalid_json_hints_qr_phase(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    with pytest.raises(SystemExit):
        qr_cli.cli(["--state-dir", str(tmp_path), "--qr-phase", "impl-code", "batch"])
    out = capsys.readouterr().out
    # Must reference the qr usage/setup, not the plan one
    assert "--qr-phase" in out


# F2: QR-fix plan-design apply prompt contains RPC METHOD CATALOG, not list-methods
def test_qr_fix_plan_design_apply_has_catalog_not_listmethods():
    from skills.planner.quality_reviewer.prompts.fix import _resolve_body, get_fix_content

    content = get_fix_content("plan-design")["apply"]
    body = _resolve_body(content, "/tmp/state")
    full = "\n".join(body)
    assert "RPC METHOD CATALOG" in full, "catalog missing from plan-design apply"
    assert "list-methods subcommand" not in full, "list-methods leak in apply prompt"


# --- Fix: parse_csv scalar fall-through (HIGH) --------------------------------

@pytest.mark.parametrize(
    ("bad_scalar",),
    [
        pytest.param(123, id="int"),
        pytest.param(True, id="bool"),
        pytest.param(1.5, id="float"),
        pytest.param({"x": 1}, id="dict"),
    ],
)
def test_parse_csv_scalar_rejection_through_set_milestone(tmp_path: Path, bad_scalar):
    """Non-string, non-list scalars hitting parse_csv raise a clean error.

    Previously a bare int/float/bool/dict would fall through to
    value.split(",") and produce a cryptic ``'X' object has no attribute 'split'``.
    Now raises ValueError with a clear type name in the message.
    """
    ctx = _seed_plan(tmp_path)
    methods = discover_methods(pc)
    with pytest.raises(ValueError, match=r"expected a string or list of strings") as exc:
        dispatch(methods, "set-milestone", {"name": "ms", "files": bad_scalar}, ctx)
    msg = str(exc.value)
    assert ".split" not in msg
    # Verify the type name is mentioned so the caller knows what was received
    assert type(bad_scalar).__name__ in msg


def test_parse_csv_none_returns_empty(tmp_path: Path):
    """None/empty input to parse_csv must still return [] (no regression)."""
    ctx = _seed_plan(tmp_path)
    methods = discover_methods(pc)
    dispatch(methods, "set-milestone", {"name": "ms", "files": None}, ctx)
    plan = json.loads(ctx.plan_path().read_text(encoding="utf-8"))
    assert plan["milestones"][0]["files"] == []


# --- Fix: documentation_only strict bool validation (MEDIUM) ------------------


@pytest.mark.parametrize(
    ("documentation_only", "expected_outcome"),
    [
        pytest.param(True, "ok", id="bool-true"),
        pytest.param(False, "ok", id="bool-false"),
        pytest.param("true", "ok", id="str-true"),
        pytest.param("false", "ok", id="str-false"),
        pytest.param("FALSE", "ok", id="str-false-case-insensitive"),
        pytest.param("yes", "error", id="str-non-boolean"),
        pytest.param(1, "error", id="int-1"),
        pytest.param(0, "error", id="int-0"),
        pytest.param(1.0, "error", id="float-1"),
        pytest.param("1", "error", id="str-numeric"),
    ],
)
def test_documentation_only_strict_typing(tmp_path: Path, documentation_only, expected_outcome):
    """documentation_only must be a strict bool (or JSON bool string); everything else rejected.

    Mirror of test_version_strict_typing: int/float/non-boolean-string all raise
    ValueError with a clear type-name message.
    """
    ctx = _seed_plan(tmp_path)
    methods = discover_methods(pc)
    try:
        dispatch(
            methods,
            "set-milestone",
            {"name": "ms", "documentation_only": documentation_only},
            ctx,
        )
        outcome = "ok"
    except ValueError:
        outcome = "error"
    assert outcome == expected_outcome, (
        f"expected {expected_outcome} for documentation_only={documentation_only!r}, got {outcome}"
    )


def test_documentation_only_false_string_stored_as_bool(tmp_path: Path):
    """documentation_only="false" must store False, not the string "false".

    Regression guard: without strict typing, the old code would pass the string
    "false" through (truthy) or coerce it via bool("false") -> True, silently
    inverting intent.
    """
    ctx = _seed_plan(tmp_path)
    methods = discover_methods(pc)
    dispatch(
        methods,
        "set-milestone",
        {"name": "ms", "documentation_only": "false", "files": "a.py"},
        ctx,
    )
    plan = json.loads(ctx.plan_path().read_text(encoding="utf-8"))
    assert plan["milestones"][0]["is_documentation_only"] is False, (
        f"expected False, got {plan['milestones'][0]['is_documentation_only']!r}"
    )


def test_documentation_only_none_omitted(tmp_path: Path):
    """documentation_only=None must leave is_documentation_only as its default (False).

    Regression guard ensuring None is treated as "not provided" rather than
    triggering validation or coercing to bool(None) = False.
    """
    ctx = _seed_plan(tmp_path)
    methods = discover_methods(pc)
    dispatch(methods, "set-milestone", {"name": "ms", "files": "a.py", "documentation_only": None}, ctx)
    plan = json.loads(ctx.plan_path().read_text(encoding="utf-8"))
    assert plan["milestones"][0]["is_documentation_only"] is False
