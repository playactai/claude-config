"""Regression tests for the 2026-06-11 planner-workflow audit fixes.

Each class guards one correctness bug from docs/planner-workflow-audit.md §3:
- #1  qr.py / qr_commands.py lock race (sentinel lock, no lost writes)
- #2  enforced QR iteration ceiling with user escalation
- #3  lenient severity ingest (no whole-run abort on "must"/"blocker")
- #4  gate routes on severity-aware on-disk state, not the --qr-status word
- #5  set-change diff via file (survives apostrophes/backslashes)
- #6  Template.safe_substitute (literal "$" in a path no longer crashes)
- #10 batch is all-or-nothing and rejects duplicate ids

And the §3b "bugs surfaced only by the run logs":
- NEW-A  cwd-fragile invocation: every emitted command carries an absolute cd
         (pin_cwd), so a drifted agent cwd no longer yields "No module named 'skills'"
- NEW-B  exec-phase QR tolerates a missing context.json (graceful), while the
         plan phase stays strict
- NEW-C  verify scripts accept --result/--status to record a verdict directly,
         so the natural one-tool guess no longer hard-fails
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from skills.lib.workflow.ast.dispatch import SubagentDispatchNode
from skills.lib.workflow.ast.dispatch_renderer import (
    _expand_template_targets,
    render_subagent_dispatch,
)
from skills.lib.workflow.prompts.step import SKILLS_DIR, pin_cwd
from skills.lib.workflow.prompts.subagent import template_dispatch
from skills.planner.cli import plan_commands, qr_commands
from skills.planner.cli import qr as qr_cli
from skills.planner.cli.dispatch import batch, discover_methods
from skills.planner.orchestrator import executor as executor_orch
from skills.planner.orchestrator import planner as planner_orch
from skills.planner.quality_reviewer.impl_code_qr_verify import ImplCodeVerify
from skills.planner.quality_reviewer.prompts.decompose import dispatch_step, format_assign_cmd
from skills.planner.quality_reviewer.qr_verify_base import (
    _record_verify_result,
    _resolve_target_item,
)
from skills.planner.shared.gates import build_gate_output
from skills.planner.shared.qr.constants import QR_ITERATION_LIMIT
from skills.planner.shared.qr.phases import is_execution_phase
from skills.planner.shared.qr.types import LoopState, QRState, QRStatus
from skills.planner.shared.qr.utils import by_blocking_severity, qr_write_lock
from skills.planner.shared.resources import render_context_file
from skills.planner.shared.schema import QRItem, SchemaValidationError, validate_state


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

    def test_validate_state_ignores_noncanonical_scratch_files(self, tmp_path: Path):
        # A decompose agent can leave bare-list scratch files in the state dir.
        # Globbing qr-*.json and validating each as a QRFile dict aborted the whole
        # run (audit #3 field evidence ab1dc60a: "input_type=list"). The canonical
        # file must still validate while non-canonical scratch files are ignored.
        _write_qr(tmp_path, "plan-code", 1, [])
        (tmp_path / "qr-items.json").write_text(json.dumps([1, 2, 3]))
        (tmp_path / "qr-items-draft.json").write_text(json.dumps([{"id": "z"}]))
        validate_state(str(tmp_path))  # must not raise

    def test_validate_state_still_aborts_on_corrupt_canonical_file(self, tmp_path: Path):
        # Restricting the glob to canonical names must NOT weaken validation of a
        # real qr-{phase}.json: a list-shaped canonical file is still a hard error.
        (tmp_path / "qr-plan-code.json").write_text(json.dumps([{"id": "bad"}]))
        with pytest.raises(SchemaValidationError):
            validate_state(str(tmp_path))


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


# =============================================================================
# §3b: bugs surfaced only by the run logs
# =============================================================================


# --- NEW-A: cwd-fragile invocation -> every emitted command carries cd --------
class TestCwdPinnedCommands:
    def test_pin_cwd_prefixes_absolute_skills_dir(self):
        out = pin_cwd("uv run python -m skills.foo --step 1")
        assert out == f"cd {SKILLS_DIR} && uv run python -m skills.foo --step 1"
        assert out.startswith("cd /")  # absolute, never relative

    def test_ast_subagent_dispatch_uses_absolute_cd(self):
        node = SubagentDispatchNode(
            agent_type="general-purpose",
            command="uv run python -m skills.x --step 1",
        )
        out = render_subagent_dispatch(node)
        # The invoke cmd is quoteattr-escaped (&& -> &amp;&amp;); parse the XML and
        # assert the *decoded* command carries the absolute cd pin -- which also
        # proves the dispatch fragment is well-formed (audit #9 sibling).
        invoke = ET.fromstring(out).find(".//invoke")
        assert invoke is not None
        assert invoke.get("cmd") == f"cd {SKILLS_DIR} && uv run python -m skills.x --step 1"
        assert "cd .claude/skills/scripts" not in out  # the relative form is gone

    def test_executor_verify_start_line_is_pinned(self, tmp_path: Path):
        _write_qr(
            tmp_path,
            "impl-code",
            1,
            [{"id": "qa-001", "scope": "*", "check": "c", "status": "TODO", "severity": "MUST"}],
        )
        out = executor_orch.format_output(4, str(tmp_path), None, False)
        assert f"Start: cd {SKILLS_DIR} && uv run python -m" in out

    def test_planner_verify_start_line_is_pinned(self, tmp_path: Path):
        _write_qr(
            tmp_path,
            "plan-code",
            1,
            [{"id": "qa-001", "scope": "*", "check": "c", "status": "TODO", "severity": "MUST"}],
        )
        out = planner_orch.format_output(9, None, str(tmp_path))
        assert isinstance(out, str)  # verify step returns str, not a GateResult
        assert f"Start: cd {SKILLS_DIR} && uv run python -m" in out

    def test_decompose_grouping_cli_prose_is_pinned(self):
        out = format_assign_cmd("/tmp/sd", "impl-code", "component-")
        assert f"cd {SKILLS_DIR} && uv run python -m skills.planner.cli.qr" in out


# --- NEW-B: exec-phase QR tolerates a missing context.json (plan stays strict) -
class TestExecContextOptional:
    def test_render_context_file_missing_raises_by_default(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="Context file not found"):
            render_context_file(tmp_path / "context.json")

    def test_render_context_file_missing_ok_returns_placeholder(self, tmp_path: Path):
        out = render_context_file(tmp_path / "context.json", missing_ok=True)
        assert "No planning context.json" in out
        # a present file is still rendered verbatim regardless of the flag
        (tmp_path / "context.json").write_text('{"k": "v"}')
        assert '"k": "v"' in render_context_file(tmp_path / "context.json", missing_ok=True)

    def test_is_execution_phase_classification(self):
        assert is_execution_phase("impl-code") is True
        assert is_execution_phase("impl-docs") is True
        assert is_execution_phase("plan-code") is False
        assert is_execution_phase("plan-design") is False
        assert is_execution_phase("nonsense") is False  # unknown -> strict default

    def test_exec_decompose_step1_without_context_does_not_raise(self, tmp_path: Path):
        guidance = dispatch_step(1, "impl-code", "m", {1: "ABSORB"}, {}, state_dir=str(tmp_path))
        assert "No planning context.json" in "\n".join(guidance["actions"])

    def test_plan_decompose_step1_without_context_still_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="Context file not found"):
            dispatch_step(1, "plan-code", "m", {1: "ABSORB"}, {}, state_dir=str(tmp_path))

    def test_exec_verify_context_step_without_context_does_not_raise(self, tmp_path: Path):
        _write_qr(
            tmp_path,
            "impl-code",
            1,
            [{"id": "qa-001", "scope": "*", "check": "c", "status": "TODO", "severity": "MUST"}],
        )
        guidance = ImplCodeVerify().get_step_guidance(
            1,
            "skills.planner.quality_reviewer.impl_code_qr_verify",
            state_dir=str(tmp_path),
            qr_item=["qa-001"],
        )
        assert "No planning context.json" in "\n".join(guidance["actions"])


# Every verify entry point shares VerifyBase._step_confirm, which now emits the
# self-recording `--result` command -- so all five must route through verify_main.
_VERIFY_MODULES = [
    ("skills.planner.quality_reviewer.plan_design_qr_verify", "plan-design"),
    ("skills.planner.quality_reviewer.plan_code_qr_verify", "plan-code"),
    ("skills.planner.quality_reviewer.plan_docs_qr_verify", "plan-docs"),
    ("skills.planner.quality_reviewer.impl_code_qr_verify", "impl-code"),
    ("skills.planner.quality_reviewer.impl_docs_qr_verify", "impl-docs"),
]


# --- NEW-C: verify scripts record verdicts via their own --result flag --------
class TestVerifyResultRecording:
    @pytest.mark.parametrize("module,phase", _VERIFY_MODULES)
    def test_every_verify_script_accepts_result_flag(self, tmp_path: Path, module, phase):
        """All five verify entry points (not just impl-*) must accept --result.

        _step_confirm emits the self-recording `--result` command for every
        phase, so every verify __main__ must route through verify_main. Before
        the completion fix the three plan-* scripts still used mode_main and
        hard-failed with 'unrecognized arguments: --result' -- the very NEW-C
        footgun, reintroduced for 3 of 5 phases.
        """
        _write_qr(
            tmp_path,
            phase,
            1,
            [{"id": "qa-001", "scope": "*", "check": "c", "status": "TODO", "severity": "MUST"}],
        )
        proc = subprocess.run(
            [
                sys.executable, "-m", module,
                "--step", "3", "--state-dir", str(tmp_path),
                "--qr-item", "qa-001", "--result", "PASS",
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parents[1]),  # scripts dir: `skills` importable
        )
        assert "unrecognized arguments" not in proc.stderr, proc.stderr
        assert proc.returncode == 0, proc.stderr
        state = json.loads((tmp_path / f"qr-{phase}.json").read_text())
        assert state["items"][0]["status"] == "PASS"

    def test_resolve_single_item(self):
        assert _resolve_target_item(3, ["a"]) == "a"

    def test_resolve_multi_item_by_confirm_step(self):
        # steps 2..2N+1 pair ANALYZE/CONFIRM; (step - 2) // 2 is the item index
        assert _resolve_target_item(3, ["a", "b"]) == "a"  # item 0 CONFIRM
        assert _resolve_target_item(5, ["a", "b"]) == "b"  # item 1 CONFIRM

    def test_resolve_multi_item_unresolvable_exits(self):
        with pytest.raises(SystemExit):
            _resolve_target_item(None, ["a", "b"])  # no step to disambiguate
        with pytest.raises(SystemExit):
            _resolve_target_item(99, ["a", "b"])  # index out of range

    def test_resolve_no_items_exits(self):
        with pytest.raises(SystemExit):
            _resolve_target_item(3, [])

    def test_record_result_writes_pass(self, tmp_path: Path, capsys):
        _write_qr(
            tmp_path,
            "impl-code",
            1,
            [{"id": "qa-001", "scope": "*", "check": "c", "status": "TODO", "severity": "MUST"}],
        )
        _record_verify_result("impl-code", 3, str(tmp_path), ["qa-001"], "PASS", None)
        assert "qa-001" in capsys.readouterr().out
        state = json.loads((tmp_path / "qr-impl-code.json").read_text())
        assert state["items"][0]["status"] == "PASS"

    def test_record_fail_requires_finding(self, tmp_path: Path):
        _write_qr(
            tmp_path,
            "impl-code",
            1,
            [{"id": "qa-001", "scope": "*", "check": "c", "status": "TODO", "severity": "MUST"}],
        )
        with pytest.raises(SystemExit):  # FAIL without --finding is rejected
            _record_verify_result("impl-code", 3, str(tmp_path), ["qa-001"], "FAIL", None)

    def test_confirm_step_prose_is_self_recording_and_pinned(self, tmp_path: Path):
        _write_qr(
            tmp_path,
            "impl-code",
            1,
            [{"id": "qa-001", "scope": "*", "check": "c", "status": "TODO", "severity": "MUST"}],
        )
        guidance = ImplCodeVerify().get_step_guidance(
            3,
            "skills.planner.quality_reviewer.impl_code_qr_verify",
            state_dir=str(tmp_path),
            qr_item=["qa-001"],
        )
        body = "\n".join(guidance["actions"])
        assert f"cd {SKILLS_DIR} && uv run python -m" in body
        assert "--result PASS" in body
        assert "--result FAIL --finding" in body
        assert "update-item" not in body  # the two-tool cli.qr split is gone here


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
