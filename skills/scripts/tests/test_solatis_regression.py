"""Regression tests for issues reported on solatis/claude-config.

Each test guards against a specific bug we inherited or could inherit from
the upstream repo. Keep tests narrow: one observable symptom each.

Covered issues:
- #19: StepHeaderNode.step must be int (not str) in incoherence.format_incoherence_output
- #22: mode_main() must not KeyError on guidance dicts that only contain "error"
- #23: Planner step 6 routes doc-only plans to step 11, skipping plan-code QR loop
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent


def _run_module(*args: str) -> subprocess.CompletedProcess:
    """Invoke `python -m <args>` from skills/scripts with a short timeout."""
    return subprocess.run(
        [sys.executable, "-m", *args],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=SCRIPTS_DIR,
    )


# ---------------------------------------------------------------------------
# Issue #19: StepHeaderNode step field is typed int; incoherence must pass int.
# ---------------------------------------------------------------------------


def test_issue_19_step_header_receives_int():
    """format_incoherence_output must construct StepHeaderNode with int step.

    Upstream passed str(step), violating the dataclass type contract. The
    renderer coerces to str so it did not crash, but mypy/type checkers flag
    the violation and future changes (e.g. removing the renderer's str() call)
    would regress silently. Lock the invariant with a direct construction check.
    """
    from skills.incoherence.incoherence import format_incoherence_output
    from skills.lib.workflow.ast.nodes import StepHeaderNode

    guidance = {"actions": ["do a thing"], "next": ""}
    output = format_incoherence_output(
        step=1, phase="DETECTION", agent_type="PARENT", guidance=guidance
    )

    # The rendered header must contain the numeric step attribute
    assert 'step="1"' in output

    # And constructing the node directly with a str must fail type expectations
    # (runtime check: renderer still converts, but the node carries an int).
    node = StepHeaderNode(title="T", script="incoherence", step=1)
    assert isinstance(node.step, int)


def test_issue_19_caller_passes_int_to_step_header(monkeypatch):
    """Guard against re-introducing step=str(step) at the call site.

    The previous test is satisfied by the node API alone — if someone regresses
    the caller to StepHeaderNode(..., step=str(step)), isinstance(node.step, int)
    still holds for the independently-constructed node below it. Spy on the
    constructor to verify the actual value the caller passes in.
    """
    from skills.incoherence import incoherence
    from skills.lib.workflow.ast import nodes

    captured: list = []
    real_init = nodes.StepHeaderNode.__init__

    def spy_init(self, *args, **kwargs):
        captured.append(kwargs.get("step", args[2] if len(args) > 2 else None))
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(nodes.StepHeaderNode, "__init__", spy_init)

    incoherence.format_incoherence_output(
        step=3,
        phase="DETECTION",
        agent_type="PARENT",
        guidance={"actions": ["x"], "next": ""},
    )

    assert captured, "StepHeaderNode was never constructed"
    assert captured[0] == 3
    assert type(captured[0]) is int, (
        f"caller passed {type(captured[0]).__name__} for step, expected int"
    )


# ---------------------------------------------------------------------------
# Issue #22: mode_main() must handle {"error": ...} guidance dicts cleanly.
# ---------------------------------------------------------------------------


def test_issue_22_mode_main_handles_error_dict():
    """Invalid-step dispatch must exit 1 with a readable error, not a traceback.

    Router scripts (plan_design.py etc.) return {"error": msg} for invalid
    input. mode_main used to KeyError on guidance_dict["title"] / ["actions"],
    surfacing a Python traceback to end users.
    """
    result = _run_module(
        "skills.planner.architect.plan_design",
        "--step",
        "99",
        "--state-dir",
        "/tmp/nonexistent-regression-xyz",
    )
    assert result.returncode == 1, f"expected exit 1, got {result.returncode}"
    assert "Traceback" not in result.stderr, "mode_main must not leak a traceback"
    assert "KeyError" not in result.stderr
    assert "Error:" in result.stderr


def test_issue_22_mode_main_unit():
    """Unit-level check: mode_main's error-dict branch exits 1 via SystemExit."""
    from skills.lib.workflow import cli

    def get_guidance(step, module_path, **kwargs):
        return {"error": f"Invalid step {step}"}

    # Simulate CLI invocation with valid argparse input but error guidance
    orig_argv = sys.argv
    try:
        sys.argv = ["plan_design.py", "--step", "1"]
        with pytest.raises(SystemExit) as exc:
            cli.mode_main(
                script_file=str(SCRIPTS_DIR / "skills/planner/architect/plan_design.py"),
                get_step_guidance=get_guidance,
                description="test",
            )
        assert exc.value.code == 1
    finally:
        sys.argv = orig_argv


# ---------------------------------------------------------------------------
# Issue #23: Step 6 gate bypasses plan-code for pure-documentation plans.
# ---------------------------------------------------------------------------


def _write_plan(state_dir: Path, milestones: list[dict]) -> None:
    """Write a minimal plan.json skeleton with the given milestones."""
    plan = {
        "schema_version": 2,
        "overview": {"problem": "x", "approach": "y"},
        "planning_context": {
            "decisions": [],
            "rejected_alternatives": [],
            "constraints": [],
            "risks": [],
        },
        "invisible_knowledge": {"system": "", "invariants": [], "tradeoffs": []},
        "milestones": milestones,
        "waves": [],
    }
    (state_dir / "plan.json").write_text(json.dumps(plan))


def test_issue_23_all_doc_only_detected(tmp_path):
    """_all_milestones_doc_only returns True only when every milestone is doc-only."""
    from skills.planner.orchestrator.planner import _all_milestones_doc_only

    _write_plan(
        tmp_path,
        [
            {"id": "M-001", "is_documentation_only": True},
            {"id": "M-002", "is_documentation_only": True},
        ],
    )
    assert _all_milestones_doc_only(str(tmp_path)) is True

    _write_plan(
        tmp_path,
        [
            {"id": "M-001", "is_documentation_only": True},
            {"id": "M-002", "is_documentation_only": False},
        ],
    )
    assert _all_milestones_doc_only(str(tmp_path)) is False

    # Empty milestones -> conservative False (keep normal flow)
    _write_plan(tmp_path, [])
    assert _all_milestones_doc_only(str(tmp_path)) is False


def test_issue_23_missing_flag_defaults_to_code_path(tmp_path):
    """Milestones without the flag are treated as code (not doc-only)."""
    from skills.planner.orchestrator.planner import _all_milestones_doc_only

    _write_plan(tmp_path, [{"id": "M-001"}])
    assert _all_milestones_doc_only(str(tmp_path)) is False


def test_issue_23_missing_plan_json_is_safe(tmp_path):
    """Missing or malformed plan.json must not raise; returns False."""
    from skills.planner.orchestrator.planner import _all_milestones_doc_only

    assert _all_milestones_doc_only(str(tmp_path)) is False

    (tmp_path / "plan.json").write_text("not json {{")
    assert _all_milestones_doc_only(str(tmp_path)) is False


def test_issue_23_step6_routes_doc_only_to_step_11(tmp_path):
    """PASS at step 6 with doc-only plan -> next command points at step 11."""
    from skills.planner.orchestrator.planner import format_output

    _write_plan(
        tmp_path,
        [
            {"id": "M-001", "is_documentation_only": True},
        ],
    )

    result = format_output(step=6, qr_status="pass", state_dir=str(tmp_path))
    # format_output returns GateResult for gate steps
    output = result.output if hasattr(result, "output") else result
    assert "--step 11" in output
    assert "plan-docs" in output.lower() or "step 11" in output


def test_issue_23_step6_routes_code_plan_to_step_7(tmp_path):
    """PASS at step 6 with at least one code milestone -> routes to step 7."""
    from skills.planner.orchestrator.planner import format_output

    _write_plan(
        tmp_path,
        [
            {"id": "M-001", "is_documentation_only": True},
            {"id": "M-002", "is_documentation_only": False},
        ],
    )

    result = format_output(step=6, qr_status="pass", state_dir=str(tmp_path))
    output = result.output if hasattr(result, "output") else result
    assert "--step 7" in output
    assert "--step 11" not in output
