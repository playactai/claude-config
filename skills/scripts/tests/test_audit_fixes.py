"""Regression tests for the 2026-06-11 planner-workflow audit fixes.

Each class guards one correctness bug from docs/planner-workflow-audit.md §3:
- #1  qr.py / qr_commands.py lock race (sentinel lock, no lost writes)
- #2  enforced QR iteration ceiling with user escalation
- #3  lenient severity ingest (no whole-run abort on "must"/"blocker")
- #4  gate routes on severity-aware on-disk state, not the --qr-status word
- #5  set-change diff via file (survives apostrophes/backslashes)
- #6  Template.safe_substitute (literal "$" in a path no longer crashes)
- #10 batch is all-or-nothing and rejects duplicate ids
"""

from __future__ import annotations

import fcntl
import json
import threading
from pathlib import Path

import pytest

from skills.lib.workflow.ast.dispatch_renderer import _expand_template_targets
from skills.lib.workflow.prompts.subagent import template_dispatch
from skills.planner.cli import plan_commands, qr_commands
from skills.planner.cli import qr as qr_cli
from skills.planner.cli.dispatch import batch, discover_methods
from skills.planner.shared.gates import build_gate_output
from skills.planner.shared.qr.constants import QR_ITERATION_LIMIT
from skills.planner.shared.qr.types import LoopState, QRState, QRStatus
from skills.planner.shared.qr.utils import by_blocking_severity, qr_write_lock
from skills.planner.shared.schema import QRItem, validate_state


def _sentinel_is_free(lock_path: Path) -> bool:
    """True if the flock sentinel can be acquired non-blocking (i.e. released)."""
    with open(lock_path, "a") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return True
        except OSError:
            return False


def _write_qr(tmp_path: Path, phase: str, iteration: int, items: list[dict]) -> None:
    (tmp_path / f"qr-{phase}.json").write_text(
        json.dumps({"phase": phase, "iteration": iteration, "items": items})
    )


def _gate(tmp_path: Path, qr: QRState, *, phase="plan-code", work_step=7, pass_step=11):
    return build_gate_output(
        module_path="m",
        qr_name="QR",
        qr=qr,
        step=10,
        work_step=work_step,
        pass_step=pass_step,
        pass_message="proceed",
        fix_target=None,
        state_dir=str(tmp_path),
        phase=phase,
    )


# --- Bug #3: strict severity Literal aborted the whole run -------------------
class TestSeverityCoercion:
    def test_lowercase_severity_coerced(self):
        assert QRItem(id="q1", scope="*", check="x", severity="must").severity == "MUST"  # type: ignore[arg-type]

    def test_unknown_severity_defaults_to_should(self):
        assert QRItem(id="q1", scope="*", check="x", severity="BLOCKER").severity == "SHOULD"  # type: ignore[arg-type]

    def test_validate_state_does_not_abort_on_bad_severity(self, tmp_path: Path):
        _write_qr(
            tmp_path,
            "plan-code",
            1,
            [{"id": "q1", "scope": "*", "check": "x", "status": "TODO", "severity": "blocker"}],
        )
        validate_state(str(tmp_path))  # must not raise

    def test_lowercase_must_still_blocks_after_de_escalation(self):
        pred = by_blocking_severity(4)  # blocking == {MUST}
        assert pred({"severity": "must"}) is True
        assert pred({"severity": "MUST"}) is True
        assert pred({"severity": "should"}) is False


# --- Bug #6: literal "$" in template crashed dispatch ------------------------
class TestTemplateDollarSafety:
    def test_template_dispatch_with_dollar_in_state_dir(self):
        state_dir = "/tmp/x$y/state"  # a literal $ baked into the template
        tmpl = f"Verify $group_id\nStart: run --state-dir {state_dir} $flags"
        out = template_dispatch(
            agent_type="quality-reviewer",
            template=tmpl,
            targets=[{"group_id": "g1", "flags": "--qr-item a"}],
            command=f"run --state-dir {state_dir} $flags",
        )
        assert "/tmp/x$y/state" in out  # literal $ survived, no ValueError
        assert "g1" in out

    def test_expand_template_targets_leaves_unmatched_dollar_literal(self):
        res = _expand_template_targets("a $v /tmp/p$q", "cmd $v /tmp/p$q", ({"v": "X"},))
        assert res[0]["prompt"] == "a X /tmp/p$q"
        assert res[0]["command"] == "cmd X /tmp/p$q"


# --- Bug #4: gate must route on severity-aware on-disk state -----------------
class TestGateSourceOfTruth:
    def test_pass_when_no_blocking_failure_despite_status_fail(self, tmp_path: Path):
        # iteration 4 blocks only MUST; a SHOULD FAIL is below threshold.
        _write_qr(
            tmp_path,
            "plan-code",
            4,
            [{"id": "q1", "scope": "*", "check": "x", "status": "FAIL", "severity": "SHOULD"}],
        )
        qr = QRState(iteration=4, state=LoopState.INITIAL, status=QRStatus.FAIL)
        out = _gate(tmp_path, qr).output
        assert "GATE RESULT: PASS" in out

    def test_fail_when_blocking_failure_despite_status_pass(self, tmp_path: Path):
        _write_qr(
            tmp_path,
            "plan-code",
            1,
            [{"id": "q1", "scope": "*", "check": "x", "status": "FAIL", "severity": "MUST"}],
        )
        qr = QRState(iteration=1, state=LoopState.RETRY, status=QRStatus.PASS)
        out = _gate(tmp_path, qr).output
        assert "GATE RESULT: FAIL" in out
        assert "--step 7" in out  # routes to the fixer (work_step)


# --- Bug #2: enforced iteration ceiling with user escalation -----------------
class TestGateIterationCap:
    def test_escalates_at_iteration_limit(self, tmp_path: Path):
        _write_qr(
            tmp_path,
            "plan-code",
            QR_ITERATION_LIMIT,
            [
                {
                    "id": "q1",
                    "scope": "*",
                    "check": "unfixable",
                    "status": "FAIL",
                    "severity": "MUST",
                    "finding": "still broken",
                }
            ],
        )
        qr = QRState(iteration=QR_ITERATION_LIMIT, state=LoopState.RETRY, status=QRStatus.FAIL)
        res = _gate(tmp_path, qr)
        assert "ITERATION LIMIT" in res.output
        assert "AskUserQuestion" in res.output
        assert "unfixable" in res.output  # unresolved finding surfaced
        assert "--step 7" not in res.output  # does NOT auto-loop to the fixer
        assert res.terminal_pass is False

    def test_no_escalation_below_limit(self, tmp_path: Path):
        _write_qr(
            tmp_path,
            "plan-code",
            QR_ITERATION_LIMIT - 1,
            [{"id": "q1", "scope": "*", "check": "x", "status": "FAIL", "severity": "MUST"}],
        )
        qr = QRState(iteration=QR_ITERATION_LIMIT - 1, state=LoopState.RETRY, status=QRStatus.FAIL)
        out = _gate(tmp_path, qr).output
        assert "ITERATION LIMIT" not in out
        assert "--step 7" in out  # still loops to the fixer


# --- Bug #10: batch is all-or-nothing and rejects duplicate ids -------------
def _init_plan(tmp_path: Path) -> plan_commands.PlanContext:
    ctx = plan_commands.PlanContext(state_dir=tmp_path)
    plan_commands.init(ctx, task="t")
    return ctx


class TestBatchTransaction:
    def test_duplicate_ids_rejected(self, tmp_path: Path):
        ctx = _init_plan(tmp_path)
        methods = discover_methods(plan_commands)
        with pytest.raises(ValueError, match="Duplicate request id"):
            batch(
                methods,
                [
                    {"method": "set-milestone", "params": {"name": "A"}, "id": 1},
                    {"method": "set-milestone", "params": {"name": "B"}, "id": 1},
                ],
                ctx,
            )

    def test_mid_batch_failure_rolls_back(self, tmp_path: Path):
        ctx = _init_plan(tmp_path)
        methods = discover_methods(plan_commands)
        before = ctx.plan_path().read_bytes()
        results = batch(
            methods,
            [
                {"method": "set-milestone", "params": {"name": "A"}, "id": 1},
                {
                    "method": "set-change",
                    "params": {"milestone": "M-999", "file": "x", "diff": "d"},
                    "id": 2,
                },
            ],
            ctx,
        )
        # Second request fails (unknown milestone) -> whole batch reverted.
        assert ctx.plan_path().read_bytes() == before
        assert results[-1]["error"]["rolled_back"] is True


# --- Bug #5: set-change diff via file survives shell-hostile content ---------
class TestDiffFile:
    def test_set_change_reads_diff_from_file_with_apostrophes(self, tmp_path: Path):
        ctx = _init_plan(tmp_path)
        plan_commands.set_milestone(ctx, name="M")
        diff_path = tmp_path / "cc.diff"
        diff_path.write_text(
            '--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-pass\n+raise ValueError("can\'t \\\\n")\n'
        )
        res = plan_commands.set_change(
            ctx, milestone="M-001", file="x.py", diff_file=str(diff_path)
        )
        assert res["operation"] == "created"
        stored = ctx.load_plan().milestones[0].code_changes[0].diff
        assert "can't" in stored
        assert "\\n" in stored  # backslash preserved verbatim


# --- Bug #1: concurrent QR writes must not be lost --------------------------
class TestQrWriteLock:
    def test_concurrent_updates_no_lost_writes(self, tmp_path: Path):
        phase = "plan-code"
        n = 24
        _write_qr(
            tmp_path,
            phase,
            1,
            [
                {"id": f"q{i}", "scope": "*", "check": "c", "status": "TODO", "severity": "MUST"}
                for i in range(n)
            ],
        )
        ctx = qr_commands.QRContext(state_dir=tmp_path, phase=phase)

        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                qr_commands.update_item(ctx, f"q{i}", "PASS")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        state = json.loads((tmp_path / f"qr-{phase}.json").read_text())
        passed = sum(1 for it in state["items"] if it["status"] == "PASS")
        assert passed == n  # every write landed; none clobbered
        assert (tmp_path / f"qr-{phase}.lock").exists()

    def test_lock_released_on_exception(self, tmp_path: Path):
        with pytest.raises(RuntimeError):
            with qr_write_lock(str(tmp_path), "plan-code"):
                raise RuntimeError("boom")
        assert _sentinel_is_free(tmp_path / "qr-plan-code.lock")


class TestQrCliUpdatePath:
    """qr.py CLI cmd_update_item: post-`with` item reference + error under lock."""

    def test_update_writes_and_reports(self, tmp_path: Path, capsys):
        _write_qr(
            tmp_path,
            "plan-code",
            1,
            [{"id": "q0", "scope": "*", "check": "c", "status": "TODO", "severity": "MUST"}],
        )
        qr_cli.cmd_update_item(str(tmp_path), "plan-code", ["q0", "--status", "PASS"])
        assert "q0" in capsys.readouterr().out  # item is in scope after the with-block
        state = json.loads((tmp_path / "qr-plan-code.json").read_text())
        assert state["items"][0]["status"] == "PASS"
        assert state["items"][0]["version"] == 2

    def test_missing_item_exits_and_releases_lock(self, tmp_path: Path):
        _write_qr(tmp_path, "plan-code", 1, [])
        with pytest.raises(SystemExit):
            qr_cli.cmd_update_item(str(tmp_path), "plan-code", ["qX", "--status", "PASS"])
        assert _sentinel_is_free(tmp_path / "qr-plan-code.lock")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
