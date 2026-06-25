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
from skills.planner.cli.dispatch import batch, discover_methods, dispatch


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


def test_dispatch_rejects_hyphenated_param_key():
    # The exact mistake the catalog prevents: decision-refs (hyphen) vs decision_refs.
    methods = discover_methods(pc)
    params = {"milestone": "M-001", "file": "a.py", "behavior": "b", "decision-refs": "DL-001"}
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
