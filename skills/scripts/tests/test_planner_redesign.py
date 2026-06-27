"""Invariants for the "rigid diffs" redesign (audit §1).

Code Intent is the durable contract; plan-time unified diffs are gone. The
planner collapses to a single plan-design QR phase (6 steps, terminal at 6);
execution implements Code Intent JIT and impl-code QR is the sole code review;
exec-docs authors all documentation; the architect builds AND renders diagrams.

Each test pins one structural guarantee so the old diff-centric model cannot
silently return.
"""

from __future__ import annotations

import json

import pytest
from conftest import write_qr  # pyright: ignore[reportMissingImports]

from skills.planner.shared.gates import GateResult


# --- Planner is 6 steps, terminal at step 6 ---------------------------------
def test_planner_has_six_steps():
    from skills.planner.orchestrator.planner import STEPS
    from skills.planner.shared.constants import PLANNER_GATE_STEPS, PLANNER_TOTAL_STEPS

    assert set(STEPS) == {1, 2, 3, 4, 5, 6}
    assert PLANNER_TOTAL_STEPS == 6
    assert PLANNER_GATE_STEPS == frozenset({6})


def test_step6_is_terminal_plan_approved_with_valid_plan(tmp_path):
    from skills.planner.orchestrator.planner import format_output

    # Write a minimal completeness-valid plan (code milestone covered by a wave)
    # and a passing qr file so the gate reaches terminal PLAN APPROVED.
    (tmp_path / "plan.json").write_text(
        json.dumps(
            {
                "overview": {"problem": "p", "approach": "a"},
                "milestones": [
                    {
                        "id": "M-001",
                        "number": 1,
                        "name": "m",
                        "files": ["a.py"],
                        "code_intents": [{"id": "CI-1", "file": "a.py", "behavior": "do x"}],
                    }
                ],
                "waves": [{"id": "W-001", "milestones": ["M-001"]}],
            }
        )
    )
    write_qr(tmp_path, "plan-design", [])
    result = format_output(6, "pass", str(tmp_path))
    assert isinstance(result, GateResult)
    assert result.terminal_pass is True
    assert "PLAN APPROVED" in result.output
    assert "--step 7" not in result.output  # no plan-code phase to route to


def test_step6_missing_plan_fails_closed(tmp_path):
    # C1 fail-closed guard: step 6 with no plan.json must sys.exit (plan is
    # required for step 2+; validate_state returns plan=None when absent).
    import subprocess
    import sys
    from pathlib import Path

    write_qr(tmp_path, "plan-design", [])
    scripts_dir = Path(__file__).parent.parent
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "skills.planner.orchestrator.planner",
            "--step",
            "6",
            "--qr-status",
            "pass",
            "--state-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=scripts_dir,
    )
    assert result.returncode != 0
    assert "plan.json not found" in (result.stdout + result.stderr)


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
        body = "\n".join(get_step_guidance(step, state_dir="/tmp/x")["actions"])
        assert "Code Intent" in body or "code_intents" in body
        assert "code_changes" not in body
        assert "implementation source" not in body.lower()  # old diff-application framing gone


# --- exec-docs authors documentation (no transcription) ---------------------
def test_exec_docs_authors_not_transcribes():
    from skills.planner.technical_writer.exec_docs_execute import STEPS, get_step_guidance

    all_body = "\n".join(
        "\n".join(get_step_guidance(step, state_dir="/tmp/x")["actions"]) for step in STEPS
    )
    assert "transcrib" not in all_body.lower()  # no comment-transcription model
    assert "sole author" in all_body.lower()  # TW authors docs directly (not "authority")
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


def test_set_intent_on_doc_only_milestone_is_rejected(tmp_path):
    # Front-line guard: adding a code intent to a doc-only milestone is rejected at
    # mutation time, so the plan can't be wedged permanently-invalid with no CLI escape.
    from skills.planner.cli import plan_commands as pc

    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="Docs", files="README.md", documentation_only=True)
    with pytest.raises(ValueError, match="documentation-only"):
        pc.set_intent(ctx, milestone="M-001", file="README.md", behavior="x")


def test_doc_only_toggle_clears_intents_to_stay_valid(tmp_path):
    # Reviewer P2: toggling a code milestone to documentation-only must not save a plan
    # that final validation then rejects (doc-only <=> no code_intents). With no
    # delete-intent op, the toggle clears the now-contradictory intents so the milestone
    # becomes genuinely doc-only and validates -- the architect is never wedged.
    from skills.planner.cli import plan_commands as pc

    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="Code", files="a.py")
    pc.set_intent(ctx, milestone="M-001", file="a.py", behavior="x")
    result = pc.set_milestone(ctx, id="M-001", documentation_only=True)
    assert result["cleared_code_intents"] == 1  # the clear is reported, not silent

    plan = ctx.load_plan()
    assert plan.milestones[0].is_documentation_only is True
    assert plan.milestones[0].code_intents == []  # contradiction removed
    assert plan.validate_completeness("plan-design") == []  # no longer wedged invalid


def test_doc_only_cli_toggle_clears_intents(tmp_path, capsys):
    # The single-CLI path (plan.py) mirrors the batch RPC: toggling doc-only on a
    # milestone with intents clears them, reports the count, and leaves a valid plan.
    from skills.planner.cli import plan as plan_cli

    plan_cli.cli(["--state-dir", str(tmp_path), "init", "--task", "t"])
    plan_cli.cli(
        ["--state-dir", str(tmp_path), "set-milestone", "--name", "Code", "--files", "a.py"]
    )
    plan_cli.cli(
        ["--state-dir", str(tmp_path), "set-intent", "--milestone", "M-001",
         "--file", "a.py", "--behavior", "x"]
    )
    capsys.readouterr()  # discard the setup output
    plan_cli.cli(
        ["--state-dir", str(tmp_path), "set-milestone", "--id", "M-001", "--documentation-only"]
    )
    out = capsys.readouterr().out
    assert "Cleared 1 code intent" in out

    plan = plan_cli.load_plan(tmp_path)
    assert plan.milestones[0].is_documentation_only is True
    assert plan.milestones[0].code_intents == []
    assert plan.validate_completeness("plan-design") == []


def test_documentation_only_is_reversible(tmp_path):
    # Two-way switch (--no-documentation-only clears it): never permanently stuck.
    from skills.planner.cli import plan_commands as pc

    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="Docs", files="README.md", documentation_only=True)
    assert ctx.load_plan().milestones[0].is_documentation_only is True
    pc.set_milestone(ctx, id="M-001", documentation_only=False)
    assert ctx.load_plan().milestones[0].is_documentation_only is False


def test_impl_code_qr_excludes_doc_only_milestones():
    # impl-code QR must not enumerate is_documentation_only milestones, or their
    # acceptance criteria become unsatisfiable MUST items that loop to the ceiling.
    from skills.planner.quality_reviewer.prompts.content import (
        IMPL_CODE_STEP_1_ABSORB as STEP_1_ABSORB,
    )
    from skills.planner.quality_reviewer.prompts.content import (
        IMPL_CODE_STEP_3_ENUMERATION as STEP_3_ENUMERATION,
    )

    combined = (STEP_1_ABSORB + STEP_3_ENUMERATION).lower()
    assert "is_documentation_only" in combined
    # The field must appear alongside an explicit exclusion verb so the QR
    # agent knows to skip doc-only milestones, not merely recognise the flag.
    assert any(
        "is_documentation_only" in seg and any(w in seg for w in ("exclud", "skip", "omit", "out of scope"))
        for seg in combined.split("\n\n")
    )


# --- code_milestones() is wired structurally into impl-code decompose ----------
def test_render_code_milestone_scope_lists_only_code_milestones(tmp_path):
    from skills.planner.cli import plan_commands as pc
    from skills.planner.quality_reviewer.prompts.decompose import render_code_milestone_scope

    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="Code", files="src/a.py")  # M-001 (code)
    pc.set_milestone(ctx, name="Docs", files="README.md", documentation_only=True)  # M-002

    scope = render_code_milestone_scope(str(tmp_path), "impl-code")
    assert "CODE MILESTONES IN SCOPE" in scope
    assert "M-001" in scope
    assert "M-002" not in scope


def test_impl_code_decompose_injects_scope_into_steps_1_and_3(tmp_path):
    from skills.planner.cli import plan_commands as pc
    from skills.planner.quality_reviewer.prompts.content import get_decompose_content
    from skills.planner.quality_reviewer.prompts.decompose import dispatch_step

    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="Code", files="src/a.py")  # M-001
    pc.set_milestone(ctx, name="Docs", files="README.md", documentation_only=True)  # M-002

    content = get_decompose_content("impl-code")
    for step in (1, 3):
        result = dispatch_step(
            step,
            "impl-code",
            "m",
            content["phase_prompts"],
            content["grouping_config"],
            state_dir=str(tmp_path),
            scope_provider=content["scope_provider"],
        )
        body = "\n".join(result["actions"])
        assert "CODE MILESTONES IN SCOPE" in body, f"step {step}"
        assert "M-001" in body and "M-002" not in body, f"step {step}"


def test_code_milestone_scope_empty_for_non_impl_code_phases(tmp_path):
    from skills.planner.cli import plan_commands as pc
    from skills.planner.quality_reviewer.prompts.decompose import render_code_milestone_scope

    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="Code", files="src/a.py")
    assert render_code_milestone_scope(str(tmp_path), "plan-design") == ""
    assert render_code_milestone_scope(str(tmp_path), "impl-docs") == ""


def test_code_milestone_scope_degrades_without_plan(tmp_path):
    from skills.planner.quality_reviewer.prompts.decompose import render_code_milestone_scope

    assert render_code_milestone_scope("", "impl-code") == ""  # no state dir
    assert render_code_milestone_scope(str(tmp_path), "impl-code") == ""  # no plan.json
    (tmp_path / "plan.json").write_text("{not valid json")
    assert render_code_milestone_scope(str(tmp_path), "impl-code") == ""  # unparseable


# --- plan_completeness_errors fails CLOSED on an empty plan by default ----------
def test_completeness_fails_closed_on_empty_milestones_by_default(tmp_path):
    # The gate veto and executor step>1 guard use the default (fail closed): a
    # milestone-less plan is a real completeness error. Only the architect router
    # passes suppress_if_no_milestones=True (first-time skeleton).
    from skills.planner.shared.schema import Overview, Plan, plan_completeness_errors

    (tmp_path / "plan.json").write_text(
        Plan(overview=Overview(problem="p", approach="a")).model_dump_json()
    )
    errs = plan_completeness_errors(str(tmp_path), "plan-design")
    assert any("at least one milestone" in e for e in errs)
    assert (
        plan_completeness_errors(str(tmp_path), "plan-design", suppress_if_no_milestones=True)
        == []
    )


# --- Doc-only deliverables are authored (exec-docs) and verified (impl-docs QR) ---
def test_doc_only_milestone_surfaces_in_plan_markdown():
    # translate_to_markdown must render the flag, or the executor (which re-derives
    # plan.json from plan.md) cannot tell a doc-only milestone from a code one.
    from skills.planner.cli.plan import translate_to_markdown
    from skills.planner.shared.schema import Milestone, Overview, Plan

    plan = Plan(
        overview=Overview(problem="p", approach="a"),
        milestones=[
            Milestone(
                id="M-001",
                number=1,
                name="Migration guide",
                files=["docs/MIGRATION.md"],
                is_documentation_only=True,
                acceptance_criteria=["documents the v1->v2 break"],
            )
        ],
    )
    md = translate_to_markdown(plan)
    assert "Documentation-only milestone" in md


def test_exec_docs_authors_doc_only_deliverables():
    from skills.planner.technical_writer.exec_docs_execute import STEPS, get_step_guidance

    body = "\n".join("\n".join(get_step_guidance(s, state_dir="/tmp/x")["actions"]) for s in STEPS)
    assert "is_documentation_only" in body
    assert "acceptance_criteria" in body  # the authoring targets


def test_impl_docs_qr_verifies_doc_only_acceptance_criteria():
    from skills.planner.quality_reviewer.prompts.content import (
        IMPL_DOCS_STEP_3_ENUMERATION as STEP_3_ENUMERATION,
    )
    from skills.planner.quality_reviewer.prompts.content import ImplDocsVerify

    assert "DOCUMENTATION-ONLY MILESTONES" in STEP_3_ENUMERATION
    macro = "\n".join(
        ImplDocsVerify().get_verification_guidance({"scope": "*", "check": "x"}, "/tmp/x")
    )
    assert "is_documentation_only == true" in macro
    deliverable = "\n".join(
        ImplDocsVerify().get_verification_guidance(
            {"scope": "milestone:M-001", "check": "acceptance criteria satisfied"}, "/tmp/x"
        )
    )
    assert "DOCUMENTATION-ONLY DELIVERABLE CHECK" in deliverable


def test_impl_docs_why_not_what_verify_guidance():
    from skills.planner.quality_reviewer.prompts.content import ImplDocsVerify

    guidance = "\n".join(
        ImplDocsVerify().get_verification_guidance(
            {"scope": "*", "check": "WHY-not-WHAT violations"}, "/tmp/x"
        )
    )
    assert "WHY-NOT-WHAT VERIFICATION:" in guidance


def test_impl_code_convention_verify_guidance():
    from skills.planner.quality_reviewer.prompts.content import ImplCodeVerify

    guidance = "\n".join(
        ImplCodeVerify().get_verification_guidance(
            {"scope": "file:x.py", "check": "CONVENTION_VIOLATION"}, "/tmp/x"
        )
    )
    assert "CONVENTION VERIFICATION:" in guidance


def test_impl_code_testing_strategy_verify_guidance():
    from skills.planner.quality_reviewer.prompts.content import ImplCodeVerify

    guidance = "\n".join(
        ImplCodeVerify().get_verification_guidance(
            {"scope": "*", "check": "TESTING_STRATEGY_VIOLATION"}, "/tmp/x"
        )
    )
    assert "TESTING-STRATEGY VERIFICATION:" in guidance


def test_plan_design_test_sweep_verify_guidance():
    from skills.planner.quality_reviewer.prompts.content import PlanDesignVerify

    guidance = "\n".join(
        PlanDesignVerify().get_verification_guidance(
            {"scope": "*", "check": "Test-sweep: M-001 coupled tests"}, "/tmp/x"
        )
    )
    assert "TEST-SWEEP VERIFICATION:" in guidance


def test_plan_design_test_sweep_not_shadowed_by_code_intent():
    # select_check_guidance is first-match; the sweep rule sits before the
    # code_intent rule so a sweep check naming "code_intent" still routes to the
    # sweep block, not the generic CODE INTENT block. Guards the ordering fix.
    from skills.planner.quality_reviewer.prompts.content import PlanDesignVerify

    guidance = "\n".join(
        PlanDesignVerify().get_verification_guidance(
            {"scope": "*", "check": "Test-sweep: code_intent CI-001 coupled tests"},
            "/tmp/x",
        )
    )
    assert "TEST-SWEEP VERIFICATION:" in guidance
    assert "CODE INTENT VERIFICATION:" not in guidance


def test_plan_design_step5_mandates_sweep_token():
    # Produce/verify coupling: the decompose severity guidance must mint the
    # "Test-sweep:" token the verify predicate keys on, else the rule is dead code.
    from skills.planner.quality_reviewer.prompts.content import PLAN_DESIGN_STEP_5_GENERATE

    assert "TEST_SWEEP_INCOMPLETE" in PLAN_DESIGN_STEP_5_GENERATE
    assert "Test-sweep:" in PLAN_DESIGN_STEP_5_GENERATE


def test_plan_design_call_site_incomplete_verify_guidance():
    # CALL_SITE_INCOMPLETE checks must route to the call-site enumeration
    # verification block, not fall through to generic guidance. Guards the
    # predicate fix that adds "call_site" matching.
    from skills.planner.quality_reviewer.prompts.content import (
        PLAN_DESIGN_STEP_5_GENERATE,
        PlanDesignVerify,
    )

    assert "CALL_SITE_INCOMPLETE" in PLAN_DESIGN_STEP_5_GENERATE
    guidance = "\n".join(
        PlanDesignVerify().get_verification_guidance(
            {"scope": "call_site_incomplete",
             "check": "CALL_SITE_INCOMPLETE: helper missing enumeration"},
            "/tmp/x",
        )
    )
    assert "CALL-SITE ENUMERATION VERIFICATION:" in guidance


def test_plan_design_call_site_incomplete_not_shadowed_by_code_intent():
    # First-match ordering: the call_site rule sits before code_intent.
    # The check text mentions "code_intent" but the CALL_SITE_INCOMPLETE
    # category token routes to call-site guidance before the broader
    # code_intent predicate can match.
    from skills.planner.quality_reviewer.prompts.content import PlanDesignVerify

    guidance = "\n".join(
        PlanDesignVerify().get_verification_guidance(
            {"scope": "call_site_incomplete",
             "check": "CALL_SITE_INCOMPLETE: code_intent CI-001 modifies helper without enumerating call sites"},
            "/tmp/x",
        )
    )
    assert "CALL-SITE ENUMERATION VERIFICATION:" in guidance
    assert "CODE INTENT VERIFICATION:" not in guidance


def test_plan_design_blast_radius_unverified_verify_guidance():
    # BLAST_RADIUS_UNVERIFIED checks must route to the gating-anchor
    # verification block, not fall through to generic guidance. Guards the
    # predicate fix that adds "blast_radius"/"blast radius" matching.
    from skills.planner.quality_reviewer.prompts.content import (
        PLAN_DESIGN_STEP_5_GENERATE,
        PlanDesignVerify,
    )

    assert "BLAST_RADIUS_UNVERIFIED" in PLAN_DESIGN_STEP_5_GENERATE
    guidance = "\n".join(
        PlanDesignVerify().get_verification_guidance(
            {"scope": "blast_radius_unverified",
             "check": "BLAST_RADIUS_UNVERIFIED: risk claims path safe without citing gate function"},
            "/tmp/x",
        )
    )
    assert "BLAST-RADIUS / GATING CLAIM VERIFICATION:" in guidance


def test_impl_code_dead_params_verify_guidance():
    # DEAD_PARAMS check routes to its block, not the broad "code quality" block.
    from skills.planner.quality_reviewer.prompts.content import (
        IMPL_CODE_STEP_5_GENERATE,
        ImplCodeVerify,
    )

    assert "DEAD_PARAMS" in IMPL_CODE_STEP_5_GENERATE
    guidance = "\n".join(
        ImplCodeVerify().get_verification_guidance(
            {"scope": "*", "check": "DEAD_PARAMS: unused parameter in foo"}, "/tmp/x"
        )
    )
    assert "DEAD PARAMS VERIFICATION:" in guidance
    assert "CODE QUALITY CHECK:" not in guidance


def test_impl_docs_stale_comments_verify_guidance():
    from skills.planner.quality_reviewer.prompts.content import (
        IMPL_DOCS_STEP_5_GENERATE,
        ImplDocsVerify,
    )

    assert "STALE_COMMENTS" in IMPL_DOCS_STEP_5_GENERATE
    guidance = "\n".join(
        ImplDocsVerify().get_verification_guidance(
            {"scope": "*", "check": "STALE_COMMENTS: docstring describes old return type"},
            "/tmp/x",
        )
    )
    assert "STALE COMMENTS VERIFICATION:" in guidance


def test_impl_docs_false_rationale_not_shadowed_by_why_what():
    # A FALSE_RATIONALE check names a rationale (~why); the rule sits before the
    # why/what rule so it isn't shadowed (select_check_guidance is first-match).
    from skills.planner.quality_reviewer.prompts.content import (
        IMPL_DOCS_STEP_5_GENERATE,
        ImplDocsVerify,
    )

    assert "FALSE_RATIONALE" in IMPL_DOCS_STEP_5_GENERATE
    guidance = "\n".join(
        ImplDocsVerify().get_verification_guidance(
            {"scope": "*", "check": "FALSE_RATIONALE: comment claims why but code differs"},
            "/tmp/x",
        )
    )
    assert "FALSE RATIONALE VERIFICATION:" in guidance
    assert "WHY-NOT-WHAT VERIFICATION:" not in guidance


def test_doc_deliverable_unsatisfied_is_must_not_should():
    # A doc-only milestone's whole purpose is its deliverable; an unproduced one is
    # knowledge loss. It must block all iterations (escalating to the user at the
    # ceiling per gates.py) rather than de-escalating to a silent pass at iteration 4+.
    from skills.planner.quality_reviewer.prompts.content import (
        IMPL_DOCS_STEP_5_GENERATE as STEP_5_GENERATE,
    )

    deliverable_idx = STEP_5_GENERATE.index("DOC_DELIVERABLE_UNSATISFIED")
    # Find the nearest tier header that precedes the token (scan backwards).
    before = STEP_5_GENERATE[:deliverable_idx]
    must_idx = before.rfind("MUST")
    should_idx = before.rfind("SHOULD")
    could_idx = before.rfind("COULD")
    assert must_idx != -1, "MUST tier header not found before DOC_DELIVERABLE_UNSATISFIED"
    # MUST header must be the closest tier header (after SHOULD and COULD).
    assert must_idx > should_idx and must_idx > could_idx, (
        "DOC_DELIVERABLE_UNSATISFIED is not in the MUST tier"
    )


# --- Terminal gate: user-accept at the iteration ceiling finalizes the plan ---
def _write_ceiling_qr(state_dir, phase="plan-design"):
    """plan.json + qr-{phase}.json at the iteration ceiling with an unresolved MUST."""
    import json

    (state_dir / "plan.json").write_text(
        json.dumps(
            {
                "overview": {"problem": "p", "approach": "a"},
                "milestones": [
                    {
                        "id": "M-001",
                        "number": 1,
                        "name": "m",
                        "files": ["a.py"],
                        "code_intents": [{"id": "CI-1", "file": "a.py", "behavior": "do x"}],
                    }
                ],
                # Completeness-valid (code milestone covered by a wave): isolates the
                # accept-findings/escalation behaviour under test from the gate's
                # structural wave-coverage veto.
                "waves": [{"id": "W-001", "milestones": ["M-001"]}],
            }
        )
    )
    write_qr(
        state_dir,
        phase,
        [
            {
                "id": "q1",
                "scope": "*",
                "check": "c",
                "status": "FAIL",
                "version": 1,
                "severity": "MUST",
                "finding": "unfixable",
            }
        ],
        iteration=5,
    )


def test_iteration_limit_escalation_emits_runnable_accept_command(tmp_path):
    from skills.planner.orchestrator.planner import format_output

    _write_ceiling_qr(tmp_path)
    result = format_output(6, "fail", str(tmp_path))
    assert isinstance(result, GateResult)
    out = result.output
    # The bug was a prose-only Accept with no command (nothing saved). The escalation
    # must now carry a runnable --accept-findings command, and not finalize on its own.
    assert "--accept-findings" in out
    assert "uv run python -m" in out
    assert result.terminal_pass is False


def test_accept_findings_yields_terminal_pass_at_ceiling(tmp_path):
    from skills.planner.orchestrator.planner import format_output

    _write_ceiling_qr(tmp_path)
    result = format_output(6, "pass", str(tmp_path), accept_findings=True)
    assert isinstance(result, GateResult)
    assert result.terminal_pass is True  # disk write covered by test_main_terminal_pass_saves_to_docs_plans
    assert "PLAN APPROVED" in result.output


# --- Terminal-pass FS write: _save_plan_to_docs writes plan.md into docs/plans/ ---
def _write_terminal_plan_json(state_dir, problem="p", approach="a"):
    (state_dir / "plan.json").write_text(
        json.dumps(
            {
                "overview": {"problem": problem, "approach": approach},
                "milestones": [
                    {"id": "M-001", "number": 1, "name": "m", "files": ["a.py"],
                     "code_intents": [{"id": "CI-1", "file": "a.py", "behavior": "do x"}]}
                ],
                "waves": [{"id": "W-001", "milestones": ["M-001"]}],
            }
        )
    )


def test_save_plan_to_docs_writes_dated_slug_file(tmp_path, monkeypatch):
    from datetime import datetime

    from skills.planner.orchestrator import planner as planner_orch
    state, repo = tmp_path / "state", tmp_path / "repo"
    state.mkdir()
    repo.mkdir()
    _write_terminal_plan_json(state, problem="Fix the login bug")
    (state / "plan.md").write_text("# Plan body\n", encoding="utf-8")
    monkeypatch.setattr(planner_orch, "_find_repo_root", lambda: repo)
    out = planner_orch._save_plan_to_docs(str(state))
    expected = repo / "docs" / "plans" / f"{datetime.now().strftime('%Y-%m-%d')}-fix-the-login-bug.md"
    assert out == expected and expected.exists()
    assert expected.read_text(encoding="utf-8") == "# Plan body\n"


def test_save_plan_to_docs_appends_collision_suffix(tmp_path, monkeypatch):
    from datetime import datetime

    from skills.planner.orchestrator import planner as planner_orch
    state, repo = tmp_path / "state", tmp_path / "repo"
    state.mkdir()
    repo.mkdir()
    _write_terminal_plan_json(state, problem="Fix the login bug")
    (state / "plan.md").write_text("# v2\n", encoding="utf-8")
    date_prefix = datetime.now().strftime("%Y-%m-%d")
    docs_plans = repo / "docs" / "plans"
    docs_plans.mkdir(parents=True)
    (docs_plans / f"{date_prefix}-fix-the-login-bug.md").write_text("# v1\n", encoding="utf-8")
    monkeypatch.setattr(planner_orch, "_find_repo_root", lambda: repo)
    out = planner_orch._save_plan_to_docs(str(state))
    assert out is not None
    assert out == docs_plans / f"{date_prefix}-fix-the-login-bug-2.md"
    assert out.read_text(encoding="utf-8") == "# v2\n"
    assert (docs_plans / f"{date_prefix}-fix-the-login-bug.md").read_text(encoding="utf-8") == "# v1\n"


def test_save_plan_to_docs_returns_none_when_plan_md_absent(tmp_path, monkeypatch, capsys):
    from skills.planner.orchestrator import planner as planner_orch
    state, repo = tmp_path / "state", tmp_path / "repo"
    state.mkdir()
    repo.mkdir()
    _write_terminal_plan_json(state)
    monkeypatch.setattr(planner_orch, "_find_repo_root", lambda: repo)
    out = planner_orch._save_plan_to_docs(str(state))
    assert out is None
    assert "plan.md not found" in capsys.readouterr().err
    assert not (repo / "docs").exists()


def test_translate_plan_renders_markdown_into_state_dir(tmp_path):
    from skills.planner.orchestrator import planner as planner_orch
    state = tmp_path / "state"
    state.mkdir()
    _write_terminal_plan_json(state)
    plan_md = planner_orch._translate_plan(str(state))
    assert plan_md == str(state / "plan.md")
    assert (state / "plan.md").read_text(encoding="utf-8").strip() != ""


def test_main_terminal_pass_saves_to_docs_plans(tmp_path, monkeypatch, capsys):
    import sys

    from skills.planner.orchestrator import planner as planner_orch
    state, repo = tmp_path / "state", tmp_path / "repo"
    state.mkdir()
    repo.mkdir()
    _write_terminal_plan_json(state, problem="Add OAuth support")
    write_qr(state, "plan-design", [])
    monkeypatch.setattr(planner_orch, "_find_repo_root", lambda: repo)
    monkeypatch.setattr(sys, "argv",
        ["planner.py", "--step", "6", "--qr-status", "pass", "--state-dir", str(state)])
    planner_orch.main()
    out = capsys.readouterr().out
    assert "PLAN APPROVED" in out and "Plan rendered to:" in out and "Plan saved to:" in out
    assert len(list((repo / "docs" / "plans").glob("*-add-oauth-support.md"))) == 1


# --- Executor validates the (LLM-authored) plan.json at step 2 ---
def test_executor_main_rejects_non_conforming_plan(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    scripts_dir = Path(__file__).parent.parent
    # int-list waves diverge from the Wave model -> validate_state must abort step 2.
    (tmp_path / "plan.json").write_text(
        json.dumps(
            {
                "overview": {"problem": "p", "approach": "a"},
                "milestones": [{"id": "M-001", "number": 1, "name": "m", "files": ["a.py"]}],
                "waves": [[0], [1, 2]],
            }
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "skills.planner.orchestrator.executor",
            "--step",
            "2",
            "--state-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=scripts_dir,
    )
    assert result.returncode != 0
    assert "validation" in (result.stdout + result.stderr).lower()


def test_executor_main_rejects_plan_missing_code_intents(tmp_path):
    # Structurally valid (Wave objects, refs resolve) but a code milestone with no
    # code_intents: validate_state passes, validate_completeness must catch the
    # dropped durable contract so the developer is never dispatched with empty intent.
    import json
    import subprocess
    import sys
    from pathlib import Path

    scripts_dir = Path(__file__).parent.parent
    (tmp_path / "plan.json").write_text(
        json.dumps(
            {
                "overview": {"problem": "p", "approach": "a"},
                "milestones": [{"id": "M-001", "number": 1, "name": "m", "files": ["a.py"]}],
                "waves": [{"id": "W-001", "milestones": ["M-001"]}],
            }
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "skills.planner.orchestrator.executor",
            "--step",
            "2",
            "--state-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=scripts_dir,
    )
    assert result.returncode != 0
    assert "completeness" in (result.stdout + result.stderr).lower()


def test_executor_step2_requires_plan_json(tmp_path):
    # Fail closed: a step>1 run whose state dir lacks plan.json must error, not
    # silently dispatch an empty implementation with no completeness check
    # (validate_state also skips an absent plan.json).
    import subprocess
    import sys
    from pathlib import Path

    scripts_dir = Path(__file__).parent.parent
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "skills.planner.orchestrator.executor",
            "--step",
            "2",
            "--state-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=scripts_dir,
    )
    assert result.returncode != 0
    assert "plan.json not found" in (result.stdout + result.stderr)


def test_wave_referencing_unknown_milestone_fails_validation(tmp_path):
    # Waves are first-class milestone cross-references; a dangling ref must abort.
    import json

    from skills.planner.shared.schema import SchemaValidationError, validate_state

    (tmp_path / "plan.json").write_text(
        json.dumps(
            {
                "overview": {"problem": "p", "approach": "a"},
                "milestones": [
                    {
                        "id": "M-001",
                        "number": 1,
                        "name": "m",
                        "files": ["a.py"],
                        "code_intents": [{"id": "CI-1", "file": "a.py", "behavior": "x"}],
                    }
                ],
                "waves": [{"id": "W-001", "milestones": ["M-999"]}],
            }
        )
    )
    with pytest.raises(SchemaValidationError, match="unknown milestone"):
        _plan, _qr_states = validate_state(str(tmp_path))


# --- Execution waves: structured, validated plan-time contract (audit §2 leak 1) ---
def test_set_wave_cli_happy_path(tmp_path):
    from skills.planner.cli import plan_commands as pc

    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="auth", files="a.py")
    pc.set_milestone(ctx, name="users", files="b.py")
    pc.set_wave(ctx, milestones="M-001")
    pc.set_wave(ctx, milestones="M-002")
    waves = ctx.load_plan().waves
    assert [(w.id, w.milestones) for w in waves] == [("W-001", ["M-001"]), ("W-002", ["M-002"])]
    # Upsert: --id replaces the wave's milestone list (architect iterates).
    pc.set_wave(ctx, id="W-001", milestones="M-001,M-002")
    assert ctx.load_plan().waves[0].milestones == ["M-001", "M-002"]


def test_set_wave_batch_mode(tmp_path):
    # set-wave is auto-discovered as a batch RPC method (no registry edit needed).
    from skills.planner.cli import plan_commands as pc
    from skills.planner.cli.dispatch import batch, discover_methods

    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="auth", files="a.py")
    methods = discover_methods(pc)
    results = batch(methods, [{"method": "set-wave", "params": {"milestones": "M-001"}, "id": 1}], ctx)
    assert results[0]["result"]["operation"] == "created"
    # Wave has no CAS field, so the result must not carry a meaningless version.
    assert "version" not in results[0]["result"]
    assert [w.milestones for w in ctx.load_plan().waves] == [["M-001"]]


def test_set_wave_accepts_valid_multi_milestone_wave(tmp_path):
    # Positive control: two file-disjoint code milestones grouped in one wave is a
    # genuinely well-formed, executable plan -- accepted and completeness-valid.
    from skills.planner.cli import plan_commands as pc

    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="auth", files="a.py")
    pc.set_milestone(ctx, name="users", files="b.py")
    pc.set_intent(ctx, milestone="M-001", file="a.py", behavior="x")
    pc.set_intent(ctx, milestone="M-002", file="b.py", behavior="y")
    pc.set_wave(ctx, milestones="M-001,M-002")

    plan = ctx.load_plan()
    assert [(w.id, w.milestones) for w in plan.waves] == [("W-001", ["M-001", "M-002"])]
    assert plan.validate_completeness("plan-design") == []  # proves acceptance of a valid wave


def test_set_wave_rejects_doc_only_milestone(tmp_path):
    # D1: doc-only milestones route to exec-docs and must be rejected at write time
    # on the RPC twin too (not only the CLI / the later executor gate). Regression for
    # the RPC-bypass whole-class miss.
    import pytest

    from skills.planner.cli import plan_commands as pc
    from skills.planner.cli.dispatch import batch, discover_methods

    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="docs", documentation_only=True)  # M-001, doc-only

    with pytest.raises(ValueError, match="cannot be added to a wave"):
        pc.set_wave(ctx, milestones="M-001")

    # The batch/RPC path must reject it too (surfaced as a rolled-back RPC error).
    methods = discover_methods(pc)
    results = batch(methods, [{"method": "set-wave", "params": {"milestones": "M-001"}, "id": 1}], ctx)
    assert "cannot be added to a wave" in results[0]["error"]["message"]
    assert ctx.load_plan().waves == []  # nothing persisted


def test_set_wave_update_unknown_id_raises(tmp_path):
    # An --id that matches no wave must error (not silently create), so a mistyped
    # update can't strand the intended wave.
    from skills.planner.cli import plan_commands as pc

    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="auth", files="a.py")
    with pytest.raises(ValueError, match="Wave W-404 not found"):
        pc.set_wave(ctx, id="W-404", milestones="M-001")
    assert ctx.load_plan().waves == []  # nothing created on the miss


def test_next_wave_id_skips_non_canonical(tmp_path):
    # A hand-authored/transcribed plan.json may carry a non-canonical wave id
    # (e.g. "W1" or "legacy"). next_wave_id must skip those instead of crashing
    # int(w.id.split('-')[1]), and derive the next id from the max canonical one
    # (max(), not len()+1, because waves can be pruned and len()+1 would collide).
    from skills.planner.shared.schema import Overview, Plan, Wave

    plan = Plan(
        overview=Overview(problem="p", approach="a"),
        waves=[
            Wave(id="W-001", milestones=[]),
            Wave(id="W1", milestones=[]),
            Wave(id="legacy", milestones=[]),
        ],
    )
    assert plan.next_wave_id() == "W-002"  # max canonical (1) + 1; no crash


def test_next_wave_id_avoids_pruned_gap_collision():
    # next_wave_id is max()-based (not len()+1) precisely so that, after a doc-only
    # toggle prunes an emptied middle wave, the next id cannot collide with a
    # surviving one: here len()+1 would yield the colliding W-003, max()+1 gives W-004.
    from skills.planner.shared.schema import Overview, Plan, Wave

    plan = Plan(
        overview=Overview(problem="p", approach="a"),
        waves=[Wave(id="W-001", milestones=["M-001"]), Wave(id="W-003", milestones=["M-003"])],
    )
    assert plan.next_wave_id() == "W-004"


def test_set_wave_create_survives_non_canonical_existing_id(tmp_path):
    # End-to-end: a create against a plan already holding a non-canonical wave id
    # returns a clean next id instead of an unhandled traceback.
    from skills.planner.cli import plan_commands as pc
    from skills.planner.shared.schema import Wave

    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="auth", files="a.py")
    plan = ctx.load_plan()
    plan.waves.append(Wave(id="W1", milestones=[]))  # non-canonical, schema-valid
    ctx.save_plan(plan)
    result = pc.set_wave(ctx, milestones="M-001")
    assert result == {"id": "W-001", "operation": "created"}


# --- Plan-design QR verify resolves code_intent-scoped items (review fix #1) ---
def test_plan_design_qr_code_intent_scope_emits_locator():
    from skills.planner.quality_reviewer.prompts.content import PlanDesignVerify

    g = "\n".join(
        PlanDesignVerify().get_verification_guidance(
            {"scope": "code_intent:CI-001", "check": "intent valid"}, "/tmp/x"
        )
    )
    assert "CODE INTENT CHECK - Focus on CI-001" in g
    assert '.milestones[].code_intents[] | select(.id == "CI-001")' in g


def test_set_wave_batch_requires_milestones(tmp_path):
    # Batch parity with the CLI's required --milestones: omitting milestones errors
    # instead of silently creating an empty wave the CLI would reject.
    from skills.planner.cli import plan_commands as pc
    from skills.planner.cli.dispatch import batch, discover_methods

    ctx = pc.PlanContext(state_dir=tmp_path)
    pc.init(ctx, task="t")
    pc.set_milestone(ctx, name="auth", files="a.py")
    methods = discover_methods(pc)
    results = batch(methods, [{"method": "set-wave", "params": {}, "id": 1}], ctx)
    assert "error" in results[0]
    assert "milestones" in results[0]["error"]["message"]
    assert ctx.load_plan().waves == []  # no empty wave persisted


def test_duplicate_wave_ids_rejected(tmp_path):
    # Two waves sharing an id (hand-authored / transcription typo): validate_state
    # rejects so update-by-id cannot silently edit one and strand the other.
    import json

    from skills.planner.shared.schema import SchemaValidationError, validate_state

    (tmp_path / "plan.json").write_text(
        json.dumps(
            {
                "overview": {"problem": "p", "approach": "a"},
                "milestones": [
                    {
                        "id": "M-001",
                        "number": 1,
                        "name": "m",
                        "files": ["a.py"],
                        "code_intents": [{"id": "CI-1", "file": "a.py", "behavior": "x"}],
                    },
                    {
                        "id": "M-002",
                        "number": 2,
                        "name": "n",
                        "files": ["b.py"],
                        "code_intents": [{"id": "CI-2", "file": "b.py", "behavior": "y"}],
                    },
                ],
                "waves": [
                    {"id": "W-002", "milestones": ["M-001"]},
                    {"id": "W-002", "milestones": ["M-002"]},
                ],
            }
        )
    )
    with pytest.raises(SchemaValidationError, match="duplicate wave id"):
        _plan, _qr_states = validate_state(str(tmp_path))


def _plan_with_waves(milestones, waves):
    from skills.planner.shared.schema import CodeIntent, Milestone, Overview, Plan, Wave

    ms = [
        Milestone(
            id=mid,
            number=i + 1,
            name=mid,
            files=files,
            is_documentation_only=doc_only,
            code_intents=(
                [] if doc_only else [CodeIntent(id=f"CI-{mid}", file=files[0], behavior="x")]
            ),
        )
        for i, (mid, files, doc_only) in enumerate(milestones)
    ]
    return Plan(
        overview=Overview(problem="p", approach="a"),
        milestones=ms,
        waves=[Wave(id=wid, milestones=mids) for wid, mids in waves],
    )


def test_wave_with_overlapping_files_rejected(tmp_path):
    # Two milestones sharing a file in one wave run as concurrent developer agents
    # and would corrupt it mid-write; validate_state must reject the overlap.
    from skills.planner.shared.schema import SchemaValidationError, validate_state

    plan = _plan_with_waves(
        [("M-001", ["a.py", "shared.py"], False), ("M-002", ["b.py", "shared.py"], False)],
        [("W-001", ["M-001", "M-002"])],
    )
    (tmp_path / "plan.json").write_text(plan.model_dump_json())
    with pytest.raises(SchemaValidationError, match="share file"):
        _plan, _qr_states = validate_state(str(tmp_path))


def test_wave_overlap_detected_across_path_spellings(tmp_path):
    # 'src/a.py' and './src/a.py' are the same physical file; the overlap guard
    # normalizes paths before intersecting, so the differing spelling can't evade
    # the concurrent-write check it exists to enforce.
    from skills.planner.shared.schema import SchemaValidationError, validate_state

    plan = _plan_with_waves(
        [("M-001", ["src/a.py"], False), ("M-002", ["./src/a.py"], False)],
        [("W-001", ["M-001", "M-002"])],
    )
    (tmp_path / "plan.json").write_text(plan.model_dump_json())
    with pytest.raises(SchemaValidationError, match="share file"):
        _plan, _qr_states = validate_state(str(tmp_path))


def test_wave_overlap_detected_across_case_variants(tmp_path):
    # On a case-insensitive checkout (macOS/Windows) 'src/App.py' and 'src/app.py'
    # resolve to one physical file; the overlap guard case-folds before intersecting
    # so two milestones differing only by case cannot co-schedule and race-write it.
    from skills.planner.shared.schema import SchemaValidationError, validate_state

    plan = _plan_with_waves(
        [("M-001", ["src/App.py"], False), ("M-002", ["src/app.py"], False)],
        [("W-001", ["M-001", "M-002"])],
    )
    (tmp_path / "plan.json").write_text(plan.model_dump_json())
    with pytest.raises(SchemaValidationError, match="share file"):
        _plan, _qr_states = validate_state(str(tmp_path))


def test_wave_overlap_detected_via_code_intent_files(tmp_path):
    # Reviewer P2: Milestone.files is empty, but two milestones' code_intents target the
    # SAME physical file (differing spellings). A developer agent is dispatched per
    # milestone and writes every file across that milestone's code_intents[], so the
    # overlap guard must union intent targets into each milestone's file set (and
    # normalize them) -- else the two milestones' developers race on shared.py mid-write,
    # the corruption this check exists to prevent.
    from skills.planner.shared.schema import (
        CodeIntent,
        Milestone,
        Overview,
        Plan,
        SchemaValidationError,
        Wave,
        validate_state,
    )

    plan = Plan(
        overview=Overview(problem="p", approach="a"),
        milestones=[
            Milestone(
                id="M-001",
                number=1,
                name="A",
                files=[],
                code_intents=[CodeIntent(id="CI-001", file="shared.py", behavior="x")],
            ),
            Milestone(
                id="M-002",
                number=2,
                name="B",
                files=[],
                code_intents=[CodeIntent(id="CI-002", file="./shared.py", behavior="y")],
            ),
        ],
        waves=[Wave(id="W-001", milestones=["M-001", "M-002"])],
    )
    (tmp_path / "plan.json").write_text(plan.model_dump_json())
    with pytest.raises(SchemaValidationError, match="share file"):
        _plan, _qr_states = validate_state(str(tmp_path))


def test_wave_overlap_detected_cross_source_files_vs_intent(tmp_path):
    # The union must catch a CROSS-SOURCE collision: M-001 declares the file in
    # Milestone.files while M-002 reaches it only via code_intents[].file. Both
    # milestones' developers would write shared.py, so co-scheduling them is still the
    # corruption case -- the guard must not require both sides to come from the same
    # field.
    from skills.planner.shared.schema import (
        CodeIntent,
        Milestone,
        Overview,
        Plan,
        SchemaValidationError,
        Wave,
        validate_state,
    )

    plan = Plan(
        overview=Overview(problem="p", approach="a"),
        milestones=[
            Milestone(
                id="M-001",
                number=1,
                name="A",
                files=["shared.py"],
                code_intents=[CodeIntent(id="CI-001", file="a.py", behavior="x")],
            ),
            Milestone(
                id="M-002",
                number=2,
                name="B",
                files=["b.py"],
                code_intents=[CodeIntent(id="CI-002", file="shared.py", behavior="y")],
            ),
        ],
        waves=[Wave(id="W-001", milestones=["M-001", "M-002"])],
    )
    (tmp_path / "plan.json").write_text(plan.model_dump_json())
    with pytest.raises(SchemaValidationError, match="share file"):
        _plan, _qr_states = validate_state(str(tmp_path))


def test_plan_gate_blocks_qr_pass_on_incomplete_plan(tmp_path):
    # A QR-pass on a plan whose code milestone is in no wave must NOT terminal-pass:
    # the gate runs the same completeness contract the executor hard-exits on and
    # routes back to the architect instead of saving an unexecutable plan (audit F1).
    from skills.planner.orchestrator.planner import format_output

    plan = _plan_with_waves([("M-001", ["a.py"], False)], [])  # code milestone, no waves
    (tmp_path / "plan.json").write_text(plan.model_dump_json())
    write_qr(tmp_path, "plan-design", [])
    result = format_output(6, "pass", str(tmp_path))
    assert isinstance(result, GateResult)
    assert result.terminal_pass is False
    assert "not assigned to any wave" in result.output
    assert "--step 3" in result.output  # routes back to the architect (work_step)


def test_plan_gate_terminal_pass_on_complete_plan(tmp_path):
    # Complement: a completeness-valid plan still reaches terminal PLAN APPROVED, so
    # the structural veto does not block legitimately-finished plans.
    from skills.planner.orchestrator.planner import format_output

    plan = _plan_with_waves([("M-001", ["a.py"], False)], [("W-001", ["M-001"])])
    (tmp_path / "plan.json").write_text(plan.model_dump_json())
    write_qr(tmp_path, "plan-design", [])
    result = format_output(6, "pass", str(tmp_path))
    assert isinstance(result, GateResult)
    assert result.terminal_pass is True
    assert "PLAN APPROVED" in result.output


def test_architect_router_surfaces_completeness_gaps_after_veto(tmp_path):
    # After the step-6 gate vetoes a QR-passing-but-incomplete plan and routes back,
    # the architect re-enters EXECUTE mode (no QR failures), so the router must list
    # the structural gaps -- otherwise the re-plan is blind and the loop has no
    # convergence pressure.
    from skills.planner.architect.plan_design import get_step_guidance

    plan = _plan_with_waves([("M-001", ["a.py"], False)], [])  # code milestone, no wave
    (tmp_path / "plan.json").write_text(plan.model_dump_json())
    guidance = get_step_guidance(1, state_dir=str(tmp_path))
    body = "\n".join(guidance["actions"])
    assert "not assigned to any wave" in body
    assert "set-wave" in body


def test_architect_router_silent_on_first_time_skeleton(tmp_path):
    # An empty skeleton is genuine first-time execution, not a repairable gap: the
    # router must NOT frame it as "approval blocked by structural gaps".
    import json

    from skills.planner.architect.plan_design import get_step_guidance

    (tmp_path / "plan.json").write_text(
        json.dumps({"overview": {"problem": "", "approach": ""}, "milestones": [], "waves": []})
    )
    guidance = get_step_guidance(1, state_dir=str(tmp_path))
    body = "\n".join(guidance["actions"])
    assert "First-time execution" in body
    assert "structural gaps" not in body


def test_exec_routers_fail_closed_without_state_dir():
    # AL2: the work-phase routers fail closed without --state-dir (matching plan_design)
    # instead of silently routing to first-time EXECUTE. The policy lives once in
    # build_route_dispatch, so all three routers agree.
    from skills.planner.developer.exec_implement import get_step_guidance as impl_router
    from skills.planner.technical_writer.exec_docs import get_step_guidance as docs_router

    assert impl_router(1) == {"error": "--state-dir required"}
    assert docs_router(1) == {"error": "--state-dir required"}


def test_build_route_dispatch_fix_mode_reuses_threaded_iteration(tmp_path):
    # E1: detect_qr_state loads qr-{phase}.json once and threads the iteration through
    # route_work_phase; build_route_dispatch reuses it for the fix-mode message rather
    # than re-reading the file. A blocking FAIL at iteration 2 surfaces "iteration 2".
    import json

    from skills.planner.shared.routing import build_route_dispatch

    (tmp_path / "qr-impl-code.json").write_text(json.dumps({
        "phase": "impl-code", "iteration": 2,
        "items": [{"id": "q1", "scope": "*", "check": "x", "status": "FAIL",
                   "finding": "boom", "severity": "MUST"}],
    }))
    result = build_route_dispatch(str(tmp_path), "impl-code", "Exec Implement")
    assert "Fix Mode" in result["title"]
    assert "iteration 2" in "\n".join(result["actions"])
    # The fix target is the shared phase-parameterized runner: the dispatched command
    # must carry --phase so it selects the right fix content.
    assert result["dispatch_to"] == "skills.planner.quality_reviewer.exec_qr_fix"
    assert "--phase impl-code" in result["next"]


def test_detect_qr_state_fails_closed_on_malformed_file(tmp_path):
    # A structurally-malformed-but-top-level-dict qr file (unhashable status/id) used to
    # raw-traceback in by_status/detect_qr_state. parse_qr_dict now rejects it -> load
    # returns None -> the router fails open to EXECUTE (same as a missing file), never crashes.
    import json

    from skills.planner.shared.routing import detect_qr_state, route_work_phase

    for body in (
        {"phase": "impl-code", "iteration": 1, "items": [{"id": "q1", "status": ["FAIL"]}]},
        {"phase": "impl-code", "iteration": 1, "items": [{"id": ["x"], "status": "FAIL"}]},
    ):
        (tmp_path / "qr-impl-code.json").write_text(json.dumps(body))
        assert detect_qr_state(str(tmp_path), "impl-code") == (False, [], 1)
        result = route_work_phase(str(tmp_path), "impl-code")
        assert result["has_failures"] is False
        assert result["failed_count"] == 0
        assert result["target_module"] == "skills.planner.developer.exec_implement_execute"


class TestExecQrFixConsolidation:
    """The three *_qr_fix.py files collapsed into one shared exec_qr_fix runner;
    --phase selects FIX_CONTENT, and each phase still emits its own content (AL1).
    """

    def _guidance(self, phase: str, step: int, tmp_path) -> dict:
        import json

        from skills.planner.quality_reviewer import exec_qr_fix

        (tmp_path / "context.json").write_text(
            json.dumps({"task": "t", "reference_docs": [], "decisions": []})
        )
        (tmp_path / f"qr-{phase}.json").write_text(
            json.dumps({"phase": phase, "iteration": 2, "items": []})
        )
        return exec_qr_fix.get_step_guidance(
            step, "skills.planner.quality_reviewer.exec_qr_fix",
            phase=phase, state_dir=str(tmp_path),
        )

    def test_impl_code_step1_banner_iteration_and_phase(self, tmp_path):
        g = self._guidance("impl-code", 1, tmp_path)
        body = "\n".join(g["actions"])
        assert "IMPLEMENTATION-FIX" in body
        assert "QR Iteration 2" in body
        assert "--phase impl-code" in g["next"]  # phase threaded into the next command

    def test_impl_docs_step2_injects_temporal(self, tmp_path):
        g = self._guidance("impl-docs", 2, tmp_path)
        assert g["title"] == "Apply Doc Fixes"
        assert "TEMPORAL REFERENCE:" in "\n".join(g["actions"])

    def test_plan_design_has_no_banner_but_loads_context(self, tmp_path):
        g = self._guidance("plan-design", 1, tmp_path)
        body = "\n".join(g["actions"])
        assert "PLANNING CONTEXT" in body          # plan-design loads context
        assert "QR Iteration 2" in body
        assert "-FIX" not in body                  # plan-design step 1 has no banner

    def test_plan_design_step3_validate_command_is_phase_scoped(self, tmp_path):
        g = self._guidance("plan-design", 3, tmp_path)
        body = "\n".join(g["actions"])
        assert "validate --phase plan-design" in body
        assert str(tmp_path) in body               # state_dir shell-quoted into the cmd

    def test_unknown_phase_rejected(self):
        from skills.planner.quality_reviewer import exec_qr_fix

        with pytest.raises((ValueError, KeyError)):
            exec_qr_fix.get_step_guidance(1, "m", phase="bogus", state_dir="/tmp/x")


def test_wave_coverage_required_for_code_milestones():
    # Completeness gate: every code milestone in exactly one wave.
    missing = _plan_with_waves(
        [("M-001", ["a.py"], False), ("M-002", ["b.py"], False)],
        [("W-001", ["M-001"])],
    )
    assert any(
        "M-002 is not assigned to any wave" in e
        for e in missing.validate_completeness("plan-design")
    )
    duplicate = _plan_with_waves(
        [("M-001", ["a.py"], False), ("M-002", ["b.py"], False)],
        [("W-001", ["M-001", "M-002"]), ("W-002", ["M-001"])],
    )
    assert any(
        "M-001 appears in multiple waves" in e
        for e in duplicate.validate_completeness("plan-design")
    )


def test_doc_only_milestone_must_not_be_in_a_wave():
    plan = _plan_with_waves(
        [("M-001", ["a.py"], False), ("M-002", ["README.md"], True)],
        [("W-001", ["M-001", "M-002"])],
    )
    assert any(
        "documentation-only milestone M-002 must not appear in a wave" in e
        for e in plan.validate_completeness("plan-design")
    )


def test_wave_coverage_happy_path_with_doc_only():
    # Code milestone covered by exactly one wave; doc-only milestone in NO wave -> valid.
    plan = _plan_with_waves(
        [("M-001", ["a.py"], False), ("M-002", ["README.md"], True)],
        [("W-001", ["M-001"])],
    )
    assert plan.validate_completeness("plan-design") == []


def test_execution_waves_render_in_markdown():
    from skills.planner.cli.plan import translate_to_markdown

    plan = _plan_with_waves(
        [("M-001", ["a.py"], False), ("M-002", ["b.py"], False)],
        [("W-001", ["M-001", "M-002"])],
    )
    md = translate_to_markdown(plan)
    assert "## Execution Waves" in md
    assert "- W-001: M-001, M-002" in md


def test_executor_step1_transcribes_waves_no_diagram_parse(tmp_path):
    # The executor must COPY the plan's explicit wave list, not re-derive waves by
    # hand-parsing an ASCII dependency diagram (audit §2 leak 1).
    from skills.planner.orchestrator import executor

    out = executor.format_output(1, str(tmp_path), None, False)
    assert "Milestone Dependencies" not in out
    assert "same depth" not in out
    assert "## Execution Waves" in out
    assert "transcribe" in out.lower() or "verbatim" in out.lower()


def test_save_plan_rolls_back_rejected_mutation(tmp_path):
    # The single-CLI save_plan validates THEN persists; a rejected mutation (here a
    # file-overlapping wave) must roll back, not leave bad state on disk + traceback.
    from skills.planner.cli.plan import save_plan
    from skills.planner.shared.schema import SchemaValidationError

    good = _plan_with_waves(
        [("M-001", ["a.py"], False), ("M-002", ["b.py"], False)],
        [("W-001", ["M-001"]), ("W-002", ["M-002"])],
    )
    save_plan(tmp_path, good)
    before = (tmp_path / "plan.json").read_bytes()

    bad = _plan_with_waves(
        [("M-001", ["a.py", "shared.py"], False), ("M-002", ["b.py", "shared.py"], False)],
        [("W-001", ["M-001", "M-002"])],
    )
    with pytest.raises(SchemaValidationError, match="share file"):
        save_plan(tmp_path, bad)
    assert (tmp_path / "plan.json").read_bytes() == before  # rolled back byte-identically


def test_save_plan_writes_nothing_on_rejected_first_write(tmp_path):
    # First-write path: validate-before-write rejects a bad mutation without ever
    # creating plan.json (no orphan, nothing to roll back).
    from skills.planner.cli.plan import save_plan
    from skills.planner.shared.schema import SchemaValidationError

    bad = _plan_with_waves(
        [("M-001", ["a.py", "shared.py"], False), ("M-002", ["b.py", "shared.py"], False)],
        [("W-001", ["M-001", "M-002"])],
    )
    with pytest.raises(SchemaValidationError, match="share file"):
        save_plan(tmp_path, bad)
    assert not (tmp_path / "plan.json").exists()  # never written, no orphan left behind


def test_milestone_listed_twice_in_wave_is_coverage_not_self_overlap():
    # A milestone listed twice in one wave must not report the confusing
    # "co-schedules M-001 and M-001" self-overlap; it surfaces as a coverage error.
    plan = _plan_with_waves([("M-001", ["a.py"], False)], [("W-001", ["M-001", "M-001"])])
    assert not any("co-schedules M-001 and M-001" in e for e in plan.validate_refs())
    assert any(
        "M-001 appears in multiple waves" in e
        for e in plan.validate_completeness("plan-design")
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
