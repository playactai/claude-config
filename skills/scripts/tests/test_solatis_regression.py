"""Regression tests for issues reported on solatis/claude-config.

Each test guards against a specific bug we inherited or could inherit from
the upstream repo. Keep tests narrow: one observable symptom each.

Covered issues:
- #19: StepHeaderNode.step must be int (not str) in incoherence.format_incoherence_output
- #22: mode_main() must not KeyError on guidance dicts that only contain "error"
- #23: Planner step 6 PASS approves the plan (terminal); doc-only milestones are
       handled at execution by exec-docs (no plan-time fast-path)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from skills.planner.shared.gates import GateResult

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
# Issue #23 (post rigid-diff redesign): plan-design is the only planning phase.
# Step 6 PASS approves the plan (terminal -- no plan-code/plan-docs to route to).
# Documentation-only milestones are handled at execution by exec-docs, not by a
# plan-time fast-path.
# ---------------------------------------------------------------------------


def _write_plan(state_dir: Path, milestones: list[dict]) -> None:
    """Write a completeness-valid plan.json with the given milestones.

    Each milestone dict may omit number/name/files; sensible defaults fill them.
    Every code milestone gets a default code_intent (unless supplied) and its own
    single-milestone wave, so the plan satisfies the step-6 gate -- which now
    enforces the same wave-coverage + intent completeness the executor does, not
    just qr state.
    """
    norm = []
    for i, ms in enumerate(milestones, 1):
        m = {"number": i, "name": ms.get("name", f"M{i}"), "files": ms.get("files", ["a.py"]), **ms}
        if not m.get("is_documentation_only") and not m.get("code_intents"):
            m["code_intents"] = [{"id": f"CI-{m.get('id', i)}", "file": m["files"][0], "behavior": "x"}]
        norm.append(m)
    # One wave per code milestone: covers every code milestone exactly once, and a
    # single milestone per wave cannot trip the intra-wave file-overlap guard.
    waves = [
        {"id": f"W-{i:03d}", "milestones": [m["id"]]}
        for i, m in enumerate(
            (m for m in norm if m.get("id") and not m.get("is_documentation_only")), 1
        )
    ]
    plan = {
        "overview": {"problem": "x", "approach": "y"},
        "planning_context": {
            "decisions": [],
            "rejected_alternatives": [],
            "constraints": [],
            "risks": [],
        },
        "invisible_knowledge": {"system": "", "invariants": [], "tradeoffs": []},
        "milestones": norm,
        "waves": waves,
    }
    (state_dir / "plan.json").write_text(json.dumps(plan))


def test_issue_23_step6_pass_is_terminal(tmp_path):
    """PASS at step 6 approves the plan: terminal, with no route to a later step."""
    from skills.planner.orchestrator.planner import format_output

    _write_plan(tmp_path, [{"id": "M-001", "is_documentation_only": False}])

    result = format_output(step=6, qr_status="pass", state_dir=str(tmp_path))
    output = result.output if isinstance(result, GateResult) else result
    assert "PLAN APPROVED" in output
    assert "--step 7" not in output
    assert "--step 11" not in output


def test_issue_23_executor_skips_doc_only_in_impl_code(tmp_path):
    """Doc-only milestones are skipped in impl-code; exec-docs authors their docs."""
    from skills.planner.orchestrator import executor

    out = executor.format_output(2, str(tmp_path), None, False)
    assert "is_documentation_only" in out
    assert "documentation phase" in out.lower()
