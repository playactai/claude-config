"""Invariants for the "rigid diffs" redesign (audit §1).

Code Intent is the durable contract; plan-time unified diffs are gone. The
planner collapses to a single plan-design QR phase (6 steps, terminal at 6);
execution implements Code Intent JIT and impl-code QR is the sole code review;
exec-docs authors all documentation; the architect builds AND renders diagrams.

Each test pins one structural guarantee so the old diff-centric model cannot
silently return.
"""

from __future__ import annotations

import pytest

from skills.planner.shared.gates import GateResult


# --- Planner is 6 steps, terminal at step 6 ---------------------------------
def test_planner_has_six_steps():
    from skills.planner.orchestrator.planner import STEPS
    from skills.planner.shared.constants import PLANNER_GATE_STEPS, PLANNER_TOTAL_STEPS

    assert set(STEPS) == {1, 2, 3, 4, 5, 6}
    assert PLANNER_TOTAL_STEPS == 6
    assert PLANNER_GATE_STEPS == frozenset({6})


def test_step6_is_terminal_plan_approved(tmp_path):
    from skills.planner.orchestrator.planner import format_output

    result = format_output(6, "pass", str(tmp_path))
    assert isinstance(result, GateResult)
    assert result.terminal_pass is True
    assert "PLAN APPROVED" in result.output
    assert "--step 7" not in result.output  # no plan-code phase to route to


# --- Only three QR phases remain --------------------------------------------
def test_qr_phases_are_three():
    from skills.planner.shared.qr.phases import QR_PHASES, get_phase_config

    assert set(QR_PHASES) == {"plan-design", "impl-code", "impl-docs"}
    for dead in ("plan-code", "plan-docs"):
        with pytest.raises(ValueError):
            get_phase_config(dead)


# --- Schema: no diff/doc entities; Code Intent is the contract ---------------
def test_schema_has_no_code_change_entities():
    import skills.planner.shared.schema as schema

    for gone in ("CodeChange", "Documentation", "ReadmeEntry"):
        assert not hasattr(schema, gone), f"{gone} should be removed from schema"

    assert "code_changes" not in schema.Milestone.model_fields
    assert "documentation" not in schema.Milestone.model_fields
    assert "code_intents" in schema.Milestone.model_fields  # the contract stays
    assert "readme_entries" not in schema.Plan.model_fields


# --- CLI: diff/doc mutation commands are gone -------------------------------
def test_cli_drops_diff_and_doc_commands():
    from skills.planner.cli.plan import COMMANDS, ROLE_PERMISSIONS

    names = {c.name for c in COMMANDS}
    for gone in (
        "set-change",
        "set-doc",
        "set-readme",
        "set-doc-diff",
        "create-doc-change",
        "list-changes",
    ):
        assert gone not in names, f"{gone} should be removed from the CLI"
    assert "set-diagram-render" in names  # survives (architect-owned)

    # No plan-phase developer/tw mutation role; diagram render is the architect's.
    assert "developer" not in ROLE_PERMISSIONS
    assert "set-diagram-render" in ROLE_PERMISSIONS["architect"]


# --- Exec dispatch carries Code Intent, not diffs ---------------------------
def test_executor_step2_dispatches_code_intent_not_diffs(tmp_path):
    from skills.planner.orchestrator import executor

    out = executor.format_output(2, str(tmp_path), None, False)
    assert "Code Intent" in out
    assert "code_changes" not in out
    assert "implementation source" not in out.lower()  # old diff-application framing gone
    # Doc-only milestones route to the documentation phase, not a developer.
    assert "is_documentation_only" in out


def test_exec_implement_execute_implements_from_intent():
    from skills.planner.developer.exec_implement_execute import get_step_guidance

    for step in (1, 2):
        body = "\n".join(get_step_guidance(step)["actions"])
        assert "Code Intent" in body or "code_intents" in body
        assert "code_changes" not in body
        assert "implementation source" not in body.lower()  # old diff-application framing gone


# --- exec-docs authors documentation (no transcription) ---------------------
def test_exec_docs_authors_not_transcribes():
    from skills.planner.technical_writer.exec_docs_execute import STEPS, get_step_guidance

    all_body = "\n".join("\n".join(get_step_guidance(step)["actions"]) for step in STEPS)
    assert "transcrib" not in all_body.lower()  # no comment-transcription model
    assert "author" in all_body.lower()  # TW authors docs directly
    assert "docstring" in all_body.lower()


# --- Architect builds AND renders diagrams; render reaches plan.md -----------
def test_architect_renders_diagrams_at_plan_design():
    from skills.planner.architect.plan_design_execute import get_step_guidance

    body = "\n".join(get_step_guidance(6)["actions"])
    assert "set-diagram-render" in body  # architect renders its own IR


def test_rendered_diagram_surfaces_in_plan_markdown():
    from skills.planner.cli.plan import translate_to_markdown
    from skills.planner.shared.schema import (
        CodeIntent,
        DiagramGraph,
        DiagramNode,
        Milestone,
        Overview,
        Plan,
    )

    plan = Plan(
        overview=Overview(problem="p", approach="a"),
        milestones=[
            Milestone(
                id="M-001",
                number=1,
                name="m",
                files=["a.py"],
                code_intents=[CodeIntent(id="CI-001", file="a.py", behavior="do x")],
            )
        ],
        diagram_graphs=[
            DiagramGraph(
                id="DIAG-001",
                type="architecture",
                scope="overview",
                title="System",
                nodes=[DiagramNode(id="n1", label="N1")],
                edges=[],
                ascii_render="+--BOXART--+",
            )
        ],
    )
    md = translate_to_markdown(plan)
    assert "+--BOXART--+" in md  # rendered diagram appears
    assert "[Diagram pending" not in md  # not an unrendered placeholder
    assert "Code Intent" in md  # the contract is rendered
    assert "Code Changes" not in md  # the diff block is gone


# --- Doc-only milestones: settable, exclusive, and excluded from impl-code QR ---
def test_set_milestone_documentation_only_is_settable_and_valid(tmp_path):
    from skills.planner.cli import plan_commands as pc

    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="Docs", files="README.md", documentation_only=True)
    plan = ctx.load_plan()
    assert plan.milestones[0].is_documentation_only is True
    # A doc-only milestone validates with NO code_intents (the exemption is reachable).
    assert plan.validate_completeness("plan-design") == []


def test_doc_only_milestone_with_code_intents_fails_validation(tmp_path):
    from skills.planner.cli import plan_commands as pc

    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="Docs", files="README.md", documentation_only=True)
    pc.set_intent(ctx, milestone="M-001", file="README.md", behavior="x")
    errors = ctx.load_plan().validate_completeness("plan-design")
    assert any("documentation-only but has code_intents" in e for e in errors)


def test_impl_code_qr_excludes_doc_only_milestones():
    # impl-code QR must not enumerate is_documentation_only milestones, or their
    # acceptance criteria become unsatisfiable MUST items that loop to the ceiling.
    from skills.planner.quality_reviewer.impl_code_qr_decompose import (
        STEP_1_ABSORB,
        STEP_3_ENUMERATION,
    )

    combined = (STEP_1_ABSORB + STEP_3_ENUMERATION).lower()
    assert "is_documentation_only" in combined


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
