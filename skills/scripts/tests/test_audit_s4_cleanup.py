"""Regression guards for the planner-workflow audit §4 structural/cleanup fixes.

Covers, one class per fix:
- QR-verify forbidden list is a single SSOT (planner == executor, no drift).
- format_step fails loud on malformed branch/next_cmd combinations.
- validate_conventions surfaces non-literal get_convention() calls (no silent skip).
- planner never interpolates an unquoted state_dir into an emitted command.
- Plan.created_at is timezone-aware (no deprecated naive utcnow).
- The two phase-parameterized QR runners serve every phase and thread --phase
  through every emitted next/record command.
"""

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from conftest import write_qr  # pyright: ignore[reportMissingImports]

import validate_conventions as vc
from skills.lib.workflow.prompts.step import format_step
from skills.planner.orchestrator import executor as executor_orch
from skills.planner.orchestrator import planner as planner_orch
from skills.planner.quality_reviewer.prompts.content import (
    DECOMPOSE_CONTENT,
    VERIFIERS,
    get_decompose_content,
    get_verifier,
)
from skills.planner.quality_reviewer.qr_decompose import get_step_guidance as decompose_guidance
from skills.planner.shared.builders import (
    QR_VERIFY_FORBIDDEN,
    format_forbidden,
    shell_quote,
)
from skills.planner.shared.schema import Overview, Plan

PHASES = ["plan-design", "impl-code", "impl-docs"]
_SCRIPTS_DIR = Path(__file__).resolve().parents[1]  # scripts dir: `skills` importable

_MUST_ITEM = {"id": "qa-001", "scope": "*", "check": "code quality", "status": "TODO", "severity": "MUST"}


# --- §4.1: forbidden-list SSOT (planner == executor) -------------------------
class TestForbiddenListSSOT:
    def test_superset_has_all_six(self):
        assert len(QR_VERIFY_FORBIDDEN) == 6
        block = format_forbidden(*QR_VERIFY_FORBIDDEN)
        assert block.count("\n  - ") == 6

    def test_both_orchestrators_emit_the_same_forbidden_block(self, tmp_path: Path):
        """The SSOT block must appear verbatim in both QR-verify dispatches.

        Re-inlining a divergent list in either orchestrator (the original §4
        defect) would make this exact block absent from one of them.
        """
        block = format_forbidden(*QR_VERIFY_FORBIDDEN)

        planner_dir = tmp_path / "plan"
        planner_dir.mkdir()
        write_qr(planner_dir, "plan-design", [_MUST_ITEM])
        planner_out = planner_orch.format_output(5, None, state_dir=str(planner_dir))
        assert isinstance(planner_out, str)  # verify step renders text, not a GateResult

        exec_dir = tmp_path / "exec"
        exec_dir.mkdir()
        write_qr(exec_dir, "impl-code", [_MUST_ITEM])
        executor_out = executor_orch.format_output(4, str(exec_dir), None, False)

        assert block in planner_out
        assert block in executor_out


# --- §4.4a: format_step validation guard -------------------------------------
class TestFormatStepGuard:
    def test_if_pass_without_if_fail_raises(self):
        with pytest.raises(ValueError, match="provided together"):
            format_step("body", if_pass="cmd")

    def test_if_fail_without_if_pass_raises(self):
        with pytest.raises(ValueError, match="provided together"):
            format_step("body", if_fail="cmd")

    def test_branching_with_next_cmd_raises(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            format_step("body", next_cmd="n", if_pass="p", if_fail="f")

    def test_valid_combinations_do_not_raise(self):
        # terminal (WORKFLOW COMPLETE), next_cmd, and branching are all valid
        assert "WORKFLOW COMPLETE" in format_step("body")
        assert "NEXT STEP" in format_step("body", next_cmd="n")
        assert "PASS" in format_step("body", if_pass="p", if_fail="f")


# --- §4.4e: validate_conventions fails loud on non-literal args ---------------
class TestValidateConventionsNonLiteral:
    def _calls(self, tmp_path: Path, source: str):
        f = tmp_path / "snippet.py"
        f.write_text(source)
        return vc.extract_convention_calls(f)

    def test_literal_call_is_validatable(self, tmp_path: Path):
        literal, opaque = self._calls(tmp_path, 'get_convention("temporal.md")\n')
        assert literal == [("temporal.md", 1)]
        assert opaque == []

    def test_non_literal_call_is_surfaced(self, tmp_path: Path):
        literal, opaque = self._calls(tmp_path, "name = 'x'\nget_convention(name)\n")
        assert literal == []
        assert opaque == [2]  # cannot be statically validated -> flagged, not skipped

    def test_main_reports_opaque_call(self, tmp_path: Path, monkeypatch, capsys):
        # A non-literal get_convention() under a role dir must fail the CI check.
        skills = tmp_path / "skills" / "developer"
        skills.mkdir(parents=True)
        (skills / "x.py").write_text("v = 'a'\nget_convention(v)\n")
        monkeypatch.setattr(vc, "__file__", str(tmp_path / "validate_conventions.py"))
        monkeypatch.setattr(vc, "get_registry", lambda: {r: {"receives": []} for r in vc.ROLE_BY_DIR.values()})
        with pytest.raises(SystemExit) as exc:
            vc.main()
        assert exc.value.code == 1
        assert "non-literal" in capsys.readouterr().out


# --- §4.4g: planner never emits an unquoted state_dir ------------------------
class TestPlannerStateDirQuoting:
    def test_no_raw_state_dir_interpolation_in_source(self):
        """Source invariant: every emitted --state-dir is shell-quoted.

        A raw `--state-dir {state_dir}` would reintroduce the copy/paste shell
        injection the executor/gates were already hardened against.
        """
        src = (
            _SCRIPTS_DIR / "skills" / "planner" / "orchestrator" / "planner.py"
        ).read_text()
        assert "--state-dir {state_dir}" not in src
        assert "--state-dir {shell_quote(state_dir)}" in src

    def test_shell_quote_quotes_metacharacters(self):
        assert shell_quote("/tmp/plan dir") == "'/tmp/plan dir'"
        assert shell_quote("/tmp/plain-path") == "/tmp/plain-path"  # no-op on safe paths
        assert shell_quote("") == "''"


# --- §4.4f: created_at is timezone-aware -------------------------------------
class TestCreatedAtTimezoneAware:
    def test_created_at_is_tz_aware(self):
        plan = Plan(overview=Overview(problem="p", approach="a"))
        assert datetime.fromisoformat(plan.created_at).tzinfo is not None


# --- §4.1: QR runner parametrization (--phase serves all phases) -------------
class TestQrRunnerParametrization:
    def test_registries_cover_every_phase(self):
        assert sorted(DECOMPOSE_CONTENT) == sorted(PHASES)
        assert sorted(VERIFIERS) == sorted(PHASES)

    def test_unknown_phase_raises(self):
        with pytest.raises(ValueError, match="Unknown QR phase"):
            get_decompose_content("bogus")
        with pytest.raises(ValueError, match="Unknown QR phase"):
            get_verifier("bogus")

    @pytest.mark.parametrize("phase", PHASES)
    def test_decompose_every_step_wellformed_and_threads_phase(self, phase: str, tmp_path: Path):
        # plan-design step 1 strictly requires context.json; provide it.
        (tmp_path / "context.json").write_text(json.dumps({"task_spec": ["x"]}))
        for step in range(1, 14):
            g = decompose_guidance(
                step,
                "skills.planner.quality_reviewer.qr_decompose",
                phase=phase,
                state_dir=str(tmp_path),
            )
            assert "error" not in g, (phase, step, g)
            assert g["title"]
            if g.get("next"):
                assert f"--phase {phase}" in g["next"], (phase, step, g["next"])

    @pytest.mark.parametrize("phase", PHASES)
    def test_verify_threads_phase_into_next_and_record(self, phase: str, tmp_path: Path):
        write_qr(tmp_path, phase, [_MUST_ITEM])
        # plan-design CONTEXT strictly requires context.json (the orchestrator
        # writes it in the plan phase); exec phases degrade gracefully.
        (tmp_path / "context.json").write_text(json.dumps({"task_spec": ["x"]}))
        verifier = get_verifier(phase)
        module = "skills.planner.quality_reviewer.qr_verify"
        # CONTEXT (step 1): NEXT STEP carries --phase
        ctx = verifier.get_step_guidance(1, module, state_dir=str(tmp_path), qr_item=["qa-001"])
        assert f"--phase {phase}" in ctx["next"]
        # CONFIRM (step 3): self-record commands AND next carry --phase
        confirm = verifier.get_step_guidance(3, module, state_dir=str(tmp_path), qr_item=["qa-001"])
        body = "\n".join(confirm["actions"])
        assert f"--phase {phase}" in body
        assert f"--phase {phase}" in confirm["next"]

    def test_verify_runner_cli_rejects_bad_phase(self, tmp_path: Path):
        proc = subprocess.run(
            [sys.executable, "-m", "skills.planner.quality_reviewer.qr_verify",
             "--phase", "bogus", "--step", "1", "--state-dir", str(tmp_path), "--qr-item", "qa-001"],
            capture_output=True, text=True, cwd=str(_SCRIPTS_DIR),
        )
        assert proc.returncode != 0
        assert "invalid choice" in proc.stderr

    def test_decompose_grouping_next_uses_runner_module(self):
        # The grouping steps' next command must target the parameterized runner
        # (so --phase round-trips), not a deleted per-phase module.
        g = decompose_guidance(
            9, "skills.planner.quality_reviewer.qr_decompose", phase="impl-code", state_dir="/tmp/x"
        )
        assert "skills.planner.quality_reviewer.qr_decompose" in g["next"]
        assert re.search(r"--phase impl-code", g["next"])
