"""Tests for the code-review follow-up fixes (audit-improvements branch).

Covers the 15 original review findings where behavior was newly added, plus the
three issues found while reviewing the applied fixes:

  new-1  QR read commands must be cache-aware inside a batch (read-after-write).
  new-2  PlanContext.save_plan must validate once on the single-call path.
  new-3  batch() response-length contract (command failure vs flush failure).
"""

from __future__ import annotations

import json

import pytest
from conftest import write_qr  # pyright: ignore[reportMissingImports]

from skills.lib import conventions
from skills.planner.cli import plan_commands as pc
from skills.planner.cli import qr_commands
from skills.planner.cli.dispatch import batch, discover_methods
from skills.planner.cli.plan_common import validate_relpath
from skills.planner.quality_reviewer.qr_verify_base import _resolve_target_item
from skills.planner.shared.qr import phases
from skills.planner.shared.qr.utils import _iteration_of, has_qr_failures_from_state
from skills.planner.shared.schema import (
    CodeIntent,
    Decision,
    DiagramGraph,
    Milestone,
    Overview,
    Plan,
)


def _plan(**kw) -> Plan:
    return Plan(overview=Overview(problem="p", approach="a"), **kw)


# ---------------------------------------------------------------------------
# #1 - entity id generation is max-based (collision-safe across a pruned gap)
# ---------------------------------------------------------------------------
def test_next_milestone_id_skips_pruned_gap():
    plan = _plan(
        milestones=[
            Milestone(id="M-001", number=1, name="a", files=["a.py"]),
            Milestone(id="M-003", number=3, name="c", files=["c.py"]),
        ]
    )
    assert plan.next_milestone_id() == "M-004"  # max(1,3)+1, not len()+1 == M-003


def test_next_intent_id_skips_gap_and_is_milestone_scoped():
    ms = Milestone(
        id="M-001",
        number=1,
        name="a",
        files=["a.py"],
        code_intents=[
            CodeIntent(id="CI-M-001-001", file="a.py", behavior="x"),
            CodeIntent(id="CI-M-001-003", file="b.py", behavior="y"),
        ],
    )
    plan = _plan(milestones=[ms])
    assert plan.next_intent_id(ms) == "CI-M-001-004"


def test_next_intent_id_not_confused_by_sibling_milestone_prefix():
    # M-1 and M-10: CI-M-10-* must not be counted toward M-1's next id.
    m1 = Milestone(
        id="M-1",
        number=1,
        name="a",
        files=["a.py"],
        code_intents=[CodeIntent(id="CI-M-1-001", file="a.py", behavior="x")],
    )
    m10 = Milestone(
        id="M-10",
        number=10,
        name="b",
        files=["b.py"],
        code_intents=[CodeIntent(id="CI-M-10-009", file="b.py", behavior="y")],
    )
    plan = _plan(milestones=[m1, m10])
    assert plan.next_intent_id(m1) == "CI-M-1-002"


def test_next_decision_id_skips_gap():
    plan = _plan()
    plan.planning_context.decisions = [
        Decision(id="DL-001", decision="a", reasoning_chain="b"),
        Decision(id="DL-003", decision="c", reasoning_chain="d"),
    ]
    assert plan.next_decision_id() == "DL-004"


def test_next_diagram_id_skips_gap():
    plan = _plan(
        diagram_graphs=[
            DiagramGraph(id="DIAG-001", type="architecture", scope="overview", title="t"),
            DiagramGraph(id="DIAG-003", type="architecture", scope="overview", title="u"),
        ]
    )
    assert plan.next_diagram_id() == "DIAG-004"


def test_create_after_gap_does_not_collide_end_to_end(tmp_path):
    # The bug: len()+1 re-issues an existing id after a gap; get_intent then
    # resolves the duplicate to the FIRST entry, shadowing the new one.
    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="auth", files="a.py")
    pc.set_intent(ctx, milestone="M-001", file="a.py", behavior="first")  # CI-M-001-001
    plan = ctx.load_plan()
    ms = plan.get_milestone("M-001")
    assert ms is not None
    ms.code_intents.append(CodeIntent(id="CI-M-001-003", file="b.py", behavior="third"))
    ctx.save_plan(plan)
    res = pc.set_intent(ctx, milestone="M-001", file="c.py", behavior="new")
    assert res["id"] == "CI-M-001-004"  # not the colliding CI-M-001-003


# ---------------------------------------------------------------------------
# #1 - validate_refs flags duplicate ids (mirrors the wave-id uniqueness check)
# ---------------------------------------------------------------------------
def test_validate_refs_flags_duplicate_entity_ids():
    plan = _plan(
        milestones=[
            Milestone(
                id="M-001",
                number=1,
                name="a",
                files=["a.py"],
                code_intents=[
                    CodeIntent(id="CI-1", file="a.py", behavior="x"),
                    CodeIntent(id="CI-1", file="a.py", behavior="y"),
                ],
            ),
            Milestone(id="M-001", number=2, name="b", files=["b.py"]),
        ],
        diagram_graphs=[
            DiagramGraph(id="DIAG-001", type="architecture", scope="overview", title="t"),
            DiagramGraph(id="DIAG-001", type="architecture", scope="overview", title="u"),
        ],
    )
    plan.planning_context.decisions = [
        Decision(id="DL-001", decision="a", reasoning_chain="b"),
        Decision(id="DL-001", decision="c", reasoning_chain="d"),
    ]
    errors = plan.validate_refs()
    assert any("duplicate milestone id 'M-001'" in e for e in errors)
    assert any("duplicate intent id 'CI-1'" in e for e in errors)
    assert any("duplicate decision id 'DL-001'" in e for e in errors)
    assert any("duplicate diagram id 'DIAG-001'" in e for e in errors)


# ---------------------------------------------------------------------------
# #2 - qr iteration int-coercion (no TypeError on a string/garbled iteration)
# ---------------------------------------------------------------------------
def test_iteration_of_coerces_and_defaults():
    assert _iteration_of({"iteration": "3"}) == 3
    assert _iteration_of({"iteration": 4}) == 4
    assert _iteration_of({"iteration": 2.9}) == 2  # float floored by int()
    assert _iteration_of({"iteration": "abc"}) == 1  # garbled -> default
    assert _iteration_of({"iteration": 0}) == 1  # 1-indexed floor
    assert _iteration_of({}) == 1
    assert _iteration_of(None) == 1


def test_has_qr_failures_tolerates_string_iteration():
    state = {"iteration": "2", "items": [{"id": "x", "severity": "MUST", "status": "FAIL"}]}
    assert has_qr_failures_from_state(state) is True  # previously raised TypeError


# ---------------------------------------------------------------------------
# #6 - QR verdict target requires a CONFIRM step (parity guard)
# ---------------------------------------------------------------------------
def test_resolve_target_item_uses_confirm_step():
    items = ["a", "b", "c"]
    assert _resolve_target_item(3, items) == "a"  # item 0 CONFIRM
    assert _resolve_target_item(5, items) == "b"  # item 1 CONFIRM
    assert _resolve_target_item(7, items) == "c"  # item 2 CONFIRM


def test_resolve_target_item_rejects_analyze_step():
    # Step 4 is item 1's ANALYZE step (parity 0); recording a verdict there must
    # not silently write to the wrong/unconfirmed item.
    with pytest.raises(SystemExit):
        _resolve_target_item(4, ["a", "b"])


def test_resolve_target_item_single_item_unambiguous():
    assert _resolve_target_item(None, ["only"]) == "only"


# ---------------------------------------------------------------------------
# #7 - a mistyped phase 'workflow' constant is rejected at the eager path
# ---------------------------------------------------------------------------
def test_validate_phase_registries_rejects_bad_workflow(monkeypatch):
    monkeypatch.setitem(phases.QR_PHASES["impl-code"], "workflow", "executer")
    monkeypatch.setattr(phases, "_registries_validated", False)
    with pytest.raises(RuntimeError, match=r"invalid workflow"):
        phases.get_phase_config("impl-code")


# ---------------------------------------------------------------------------
# #3/#4/#5 - REGISTRY parser: fail loud on data it can't represent,
#            skip benign document markers
# ---------------------------------------------------------------------------
def test_parse_registry_rejects_unrecognized_key():
    with pytest.raises(ValueError, match=r"Unrecognized REGISTRY.yaml key"):
        conventions._parse_registry("developer:\n  recieves:\n    - temporal.md\n")


def test_parse_registry_rejects_flow_style_list():
    with pytest.raises(ValueError, match=r"Flow-style value not supported"):
        conventions._parse_registry("developer:\n  receives: [a, b]\n")


def test_parse_registry_rejects_flow_style_phase_specific():
    # The fail-closed guard covers every block-style container, not just receives:
    # an inline phase_specific/mode_specific value would otherwise silently drop.
    with pytest.raises(ValueError, match=r"Flow-style value not supported"):
        conventions._parse_registry(
            "developer:\n  receives: []\n  phase_specific: [a, b, c]\n"
        )


def test_parse_registry_empty_receives_is_ok():
    reg = conventions._parse_registry("developer:\n  receives: []\n")
    assert reg["developer"]["receives"] == []


def test_parse_registry_skips_document_markers():
    reg = conventions._parse_registry('---\ndeveloper:\n  receives: []\n  rationale: "x"\n...\n')
    assert reg["developer"]["rationale"] == "x"


# ---------------------------------------------------------------------------
# #10 - assign-group validates group-id before file-exists (parity with RPC)
# ---------------------------------------------------------------------------
def test_cmd_assign_group_validates_id_before_file_exists(tmp_path, capsys):
    from skills.planner.cli import qr as qr_cli

    with pytest.raises(SystemExit):
        qr_cli.cmd_assign_group(str(tmp_path), "impl-code", ["q1", "--group-id", "bogus"])
    out = capsys.readouterr().out
    assert "Invalid group_id" in out
    assert "state file not found" not in out


# ---------------------------------------------------------------------------
# #13 - validate_relpath rejects '..' COMPONENTS, not '..'-prefixed names
# ---------------------------------------------------------------------------
def test_validate_relpath_accepts_dotdot_prefixed_filename():
    assert validate_relpath("..config.py", "ctx") == "..config.py"


@pytest.mark.parametrize("bad", ["../x.py", "a/../../b.py", ".."])
def test_validate_relpath_rejects_parent_components(bad):
    with pytest.raises(ValueError, match=r"Parent-relative|current directory"):
        validate_relpath(bad, "ctx")


def test_validate_relpath_normalizes_inner_dotdot():
    assert validate_relpath("a/../b.py", "ctx") == "b.py"


# ---------------------------------------------------------------------------
# #14 - set-wave CREATE rejects empty milestones; UPDATE-to-empty still allowed
# ---------------------------------------------------------------------------
def test_set_wave_create_rejects_empty_milestones(tmp_path):
    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="auth", files="a.py")
    with pytest.raises(ValueError, match=r"required for create"):
        pc.set_wave(ctx, milestones="")
    assert ctx.load_plan().waves == []


def test_set_wave_update_to_empty_still_allowed(tmp_path):
    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="auth", files="a.py")
    pc.set_wave(ctx, milestones="M-001")
    pc.set_wave(ctx, id="W-001", milestones="")
    assert ctx.load_plan().waves[0].milestones == []


# ---------------------------------------------------------------------------
# #15 - discover_methods exposes only module-defined functions, not imports
# ---------------------------------------------------------------------------
def test_discover_methods_excludes_imported_helpers():
    plan_methods = discover_methods(pc)
    qr_methods = discover_methods(qr_commands)
    assert "set-milestone" in plan_methods
    assert "update-item" in qr_methods
    assert all(f.__module__ == pc.__name__ for f in plan_methods.values())
    assert all(f.__module__ == qr_commands.__name__ for f in qr_methods.values())


# ---------------------------------------------------------------------------
# new-1 - QR read command in a batch sees writes cached earlier in that batch
# ---------------------------------------------------------------------------
def test_qr_batch_read_after_write_sees_cache(tmp_path):
    write_qr(tmp_path, "impl-code", [{"id": "q1", "severity": "MUST", "status": "TODO"}])
    ctx = qr_commands.QRContext(state_dir=tmp_path, phase="impl-code")
    methods = discover_methods(qr_commands)
    results = batch(
        methods,
        [
            {"method": "update-item", "params": {"item_id": "q1", "status": "PASS"}, "id": 1},
            {"method": "summary", "params": {}, "id": 2},
        ],
        ctx,
    )
    assert results[1]["result"]["counts"]["PASS"] == 1  # cache-aware, not stale TODO
    assert results[1]["result"]["counts"]["TODO"] == 0
    persisted = json.loads((tmp_path / "qr-impl-code.json").read_text())
    assert persisted["items"][0]["status"] == "PASS"


# ---------------------------------------------------------------------------
# new-2 / #11 / #12 - batch writes the state file exactly once (at flush)
# ---------------------------------------------------------------------------
def test_plan_batch_writes_state_once(tmp_path, monkeypatch):
    from skills.planner.cli import plan_common

    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="m0", files="a.py")  # setup write, before the spy

    writes = {"n": 0}
    real = plan_common.atomic_write_text

    def counting(path, text):
        writes["n"] += 1
        return real(path, text)

    monkeypatch.setattr(plan_common, "atomic_write_text", counting)
    methods = discover_methods(pc)
    results = batch(
        methods,
        [
            {"method": "set-milestone", "params": {"name": "m1", "files": "b.py"}, "id": 1},
            {"method": "set-milestone", "params": {"name": "m2", "files": "c.py"}, "id": 2},
            {"method": "set-decision", "params": {"decision": "d", "reasoning": "r"}, "id": 3},
        ],
        ctx,
    )
    assert all("result" in r for r in results)
    assert writes["n"] == 1  # one flush write for the whole batch, not one per request
    assert {m.name for m in ctx.load_plan().milestones} == {"m0", "m1", "m2"}


def test_qr_batch_writes_state_once(tmp_path, monkeypatch):
    write_qr(
        tmp_path,
        "impl-code",
        [
            {"id": "q1", "severity": "MUST", "status": "TODO"},
            {"id": "q2", "severity": "MUST", "status": "TODO"},
        ],
    )
    ctx = qr_commands.QRContext(state_dir=tmp_path, phase="impl-code")
    writes = {"n": 0}
    real = qr_commands.save_qr_state_atomic

    def counting(path, state):
        writes["n"] += 1
        return real(path, state)

    monkeypatch.setattr(qr_commands, "save_qr_state_atomic", counting)
    methods = discover_methods(qr_commands)
    results = batch(
        methods,
        [
            {"method": "update-item", "params": {"item_id": "q1", "status": "PASS"}, "id": 1},
            {"method": "update-item", "params": {"item_id": "q2", "status": "PASS"}, "id": 2},
        ],
        ctx,
    )
    assert all("result" in r for r in results)
    assert writes["n"] == 1


# ---------------------------------------------------------------------------
# #11 / #12 - a mid-batch failure rolls back; nothing is persisted
# ---------------------------------------------------------------------------
def test_plan_batch_rolls_back_on_failure(tmp_path):
    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="m0", files="a.py")
    before = (tmp_path / "plan.json").read_text()

    methods = discover_methods(pc)
    results = batch(
        methods,
        [
            {"method": "set-milestone", "params": {"name": "m1", "files": "b.py"}, "id": 1},
            # UPDATE a milestone that does not exist -> raises -> whole batch rolls back
            {"method": "set-milestone", "params": {"id": "M-404", "name": "x"}, "id": 2},
        ],
        ctx,
    )
    assert results[0].get("rolled_back") is True
    assert "error" in results[1]
    assert (tmp_path / "plan.json").read_text() == before  # nothing persisted
    assert {m.name for m in ctx.load_plan().milestones} == {"m0"}


# ---------------------------------------------------------------------------
# new-2 - single-call save_plan validates exactly once (no double validate)
# ---------------------------------------------------------------------------
def test_save_plan_non_batch_validates_once(tmp_path, monkeypatch):
    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    plan = ctx.load_plan()

    calls = {"n": 0}
    real = Plan.validate_refs

    def counting(self):
        calls["n"] += 1
        return real(self)

    monkeypatch.setattr(Plan, "validate_refs", counting)
    ctx.save_plan(plan)
    assert calls["n"] == 1  # write_plan validates once; save_plan no longer re-validates


# ---------------------------------------------------------------------------
# F3 - a read-only batch must not rewrite the state file
# ---------------------------------------------------------------------------
def test_qr_read_only_batch_does_not_write(tmp_path, monkeypatch):
    write_qr(tmp_path, "impl-code", [{"id": "q1", "severity": "MUST", "status": "TODO"}])
    ctx = qr_commands.QRContext(state_dir=tmp_path, phase="impl-code")
    writes = {"n": 0}
    real = qr_commands.save_qr_state_atomic

    def counting(path, state):
        writes["n"] += 1
        return real(path, state)

    monkeypatch.setattr(qr_commands, "save_qr_state_atomic", counting)
    methods = discover_methods(qr_commands)
    results = batch(methods, [{"method": "summary", "params": {}, "id": 1}], ctx)
    assert "result" in results[0]
    assert writes["n"] == 0  # no save_* in the batch -> flush must not write


# ---------------------------------------------------------------------------
# F4 / new-3 - a flush failure rolls back, reports an id=None entry, and the
#              context is not left poisoned (cache cleared in finally)
# ---------------------------------------------------------------------------
def test_batch_flush_failure_reports_and_clears_cache(tmp_path, monkeypatch):
    from skills.planner.cli import plan_common

    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="m0", files="a.py")
    before = (tmp_path / "plan.json").read_text()

    real = plan_common.atomic_write_text
    state = {"n": 0}

    def flaky(path, text):
        state["n"] += 1
        if state["n"] == 1:  # fail the single flush write only
            raise OSError("disk full")
        return real(path, text)

    monkeypatch.setattr(plan_common, "atomic_write_text", flaky)
    methods = discover_methods(pc)
    results = batch(
        methods,
        [{"method": "set-milestone", "params": {"name": "m1", "files": "b.py"}, "id": 1}],
        ctx,
    )
    # every command succeeded but the flush failed: len(responses) == len(requests) + 1,
    # the command result is flagged rolled_back, and the extra entry has id=None.
    assert len(results) == 2
    assert results[0]["rolled_back"] is True
    assert results[1]["id"] is None
    assert "flush failed" in results[1]["error"]["message"]
    assert ctx._batch is None  # finally cleared the cache; context is reusable
    assert (tmp_path / "plan.json").read_text() == before  # nothing persisted


# ---------------------------------------------------------------------------
# F5 / F1 - a batch against a missing state file is a structured error, not a crash
# ---------------------------------------------------------------------------
def test_plan_batch_missing_file_raises_valueerror(tmp_path):
    """Non-init batch w/o plan.json: command fails, not batch-level crash.

    begin_batch tolerates a missing plan.json (init may create it in the same
    batch). A non-init command that needs the plan fails at dispatch time and
    is surfaced as a command-level error entry, not a ValueError escape.
    """
    ctx = pc.PlanContext(state_dir=tmp_path)  # no init -> plan.json absent
    methods = discover_methods(pc)
    results = batch(methods, [{"method": "list-milestones", "params": {}, "id": 7}], ctx)
    assert len(results) == 1
    assert results[0]["id"] == 7
    assert "error" in results[0]


def test_plan_batch_init_creates_first_plan(tmp_path):
    """An init batch must succeed even when plan.json doesn't exist yet."""
    ctx = pc.PlanContext(state_dir=tmp_path)
    methods = discover_methods(pc)
    results = batch(methods, [{"method": "init", "params": {"task": "test"}, "id": 1}], ctx)
    assert len(results) == 1
    assert results[0]["result"]["operation"] == "created"
    assert (tmp_path / "plan.json").exists()
