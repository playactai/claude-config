#!/usr/bin/env python3
"""
Plan Executor - Execute approved plans through delegation.

Twelve-step workflow with parallel QR verification:
  1. Execution Planning - analyze plan, transcribe wave list, create state_dir
  2. Implementation - dispatch developers (wave-aware parallel)
  3. Code QR Decompose - generate verification items
  4. Code QR Verify - parallel verification of items
  5. Code QR Gate - route pass/fail
  6. Documentation - TW pass
  7. Doc QR Decompose - generate verification items
  8. Doc QR Verify - parallel verification of items
  9. Doc QR Gate - route pass/fail
  10. Final Verification - run full suite/lint/type, record verify.json
  11. Final Verification Gate - read verify.json; green -> retrospective,
      red -> reset QR state + back to step 2 (re-review the fix), ceiling -> user
  12. Retrospective - present summary

QR Block Pattern (matching planner's 4-step pattern per phase):
  N   work        developer/TW agents     Implementation or documentation
  N+1 decompose   1 QR agent              qr-{phase}.json
  N+2 verify      N QR agents (parallel)  Each: PASS or FAIL
  N+3 route       0 agents (orchestrator) Loop to N or proceed to N+4
"""

import argparse
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from skills.lib.workflow.prompts import subagent_dispatch
from skills.lib.workflow.prompts.step import SKILLS_DIR, format_step, pin_cwd
from skills.planner.shared.builders import (
    ESCALATE_HANDLER,
    THINKING_EFFICIENCY,
    build_fix_mode_dispatch,
    build_qr_decompose_dispatch,
    build_qr_verify_dispatch,
    format_forbidden,
    format_gate_result,
    shell_quote,
)
from skills.planner.shared.constants import (
    EXECUTOR_GATE_CONFIG,
    EXECUTOR_STEP_PHASES,
    PHASE_QR_NAME,
)
from skills.planner.shared.constraints import (
    ORCHESTRATOR_CONSTRAINT,
)
from skills.planner.shared.gates import _render_iteration_limit_banner, build_gate_output
from skills.planner.shared.qr.cli import add_qr_args
from skills.planner.shared.qr.constants import QR_ITERATION_LIMIT
from skills.planner.shared.qr.phases import get_phase_config
from skills.planner.shared.qr.types import LoopState, QRState
from skills.planner.shared.qr.utils import (
    prepare_verify_items,
    qr_file_exists,
    resolve_qr_for_step,
)
from skills.planner.shared.resources import get_mode_script_path
from skills.planner.shared.verify_state import (
    format_verify_failures_for_fix,
    load_verify_state,
    reset_qr_for_reverify,
    verify_failures,
    verify_has_failures,
    verify_is_complete,
    verify_path,
)

if TYPE_CHECKING:
    from skills.planner.shared.schema import Plan, VerifyFile

# Module path for -m invocation
MODULE_PATH = "skills.planner.orchestrator.executor"


# =============================================================================
# Step 1: Execution Planning
# =============================================================================


def format_step_1(state_dir: str, reconciliation_check: bool) -> str:
    """Create state_dir, analyze plan, transcribe wave list."""
    actions = [
        THINKING_EFFICIENCY,
        "",
        "Plan file: $PLAN_FILE (substitute from context)",
        "",
        "ANALYZE plan:",
        "  - Read the milestones and the plan's '## Execution Waves' list",
        "  - Set up TodoWrite tracking",
        "",
        "EXECUTION WAVES (transcribe, do NOT derive):",
        "  The approved plan declares execution waves under its '## Execution Waves'",
        "  heading, e.g.:",
        "    - W-001: M-001",
        "    - W-002: M-002, M-003",
        "  Copy that list VERBATIM into plan.json.waves (objects, below). Do NOT infer",
        "  waves from any diagram or re-group milestones -- the waves are the approved",
        "  contract (file-disjoint within a wave, validated at plan time).",
        "",
        "STATE SETUP:",
        f"  State directory created: {state_dir}",
        f"  Write plan context to: {state_dir}/plan.json",
        "",
        "  After analyzing the plan, use the Write tool to create plan.json. It must",
        "  conform to the plan schema -- each milestone carries its code_intents (the",
        "  durable contract) and is_documentation_only flag; waves are objects:",
        "  {",
        '    "overview": {"problem": "<from plan>", "approach": "<from plan>"},',
        '    "milestones": [',
        '      {"id": "M-001", "number": 1, "name": "...", "files": ["..."],',
        '       "acceptance_criteria": ["..."],',
        '       "code_intents": [',
        '         {"id": "CI-M-001-001", "file": "...", "behavior": "...", "decision_refs": []}',
        "       ],",
        '       "is_documentation_only": false}',
        "    ],",
        '    "waves": [{"id": "W-001", "milestones": ["M-001"]}]',
        "  }",
        "",
        "  Transcribe code_intents and is_documentation_only faithfully from the plan;",
        "  a documentation-only milestone has is_documentation_only:true and NO",
        "  code_intents (exec-docs authors its deliverables at step 6).",
        "",
        "  Do NOT add planning_context.rejected_alternatives, planning_context.constraints,",
        "  planning_context.risks, or diagram_graphs to this plan.json -- no executor step",
        "  reads any of them, and all four are already durably captured in the approved",
        "  plan.md ($PLAN_FILE) for anything that needs them (e.g. the final review reads",
        "  $PLAN_FILE directly). Hand-retyping them from memory only adds a schema-mismatch",
        "  risk with no reader on the other end.",
        "",
        "  DECISIONS EXCEPTION: when a code_intent carries decision_refs (the durable",
        "  contract shown to developer sub-agents), include a top-level planning_context",
        "  holding ONLY the Decision entries those refs point to -- matching ids, each",
        "  entry with id, decision, and reasoning_chain, under planning_context.decision_log",
        "  -- so validate_refs resolves them. Do NOT copy the full decision log; a",
        "  code_intent with decision_refs: [] needs no planning_context at all.",
        "",
        "WORKFLOW:",
        "  This step is ANALYSIS + STATE SETUP. Do NOT delegate yet.",
        "  Record wave groupings for step 2 (Implementation).",
    ]

    if reconciliation_check:
        actions.extend(
            [
                "",
                "RECONCILIATION CHECK REQUESTED (resuming a partially-completed plan):",
                "  Determine which milestones are ALREADY satisfied so completed work is",
                "  skipped and only remaining milestones execute.",
                "",
                "  KEY DISTINCTION -- validate REQUIREMENTS, not code presence:",
                "    - Code may exist but NOT meet the criteria (done wrong).",
                "    - Criteria may be met by DIFFERENT code than planned (done correctly).",
                "",
                "  For EACH milestone, run a factored check (resist confirmation bias):",
                "    1. EXTRACT its acceptance criteria into a checklist (do not evaluate yet).",
                "    2. For each criterion, STATE what you expect, then SEARCH the codebase",
                "       (Grep/Read) with OPEN questions -- 'what is the retry threshold?', NOT",
                "       'is the threshold 3?' -- and verify BEHAVIOR, not just that code exists.",
                "    3. RECORD MET | NOT_MET with evidence (file:line, or 'not found').",
                "",
                "  Mark a milestone complete ONLY when ALL its criteria are MET; if any is",
                "  NOT_MET, execute that milestone (fully, or just the missing parts).",
            ]
        )

    body = "\n".join(actions)
    next_cmd = f"uv run python -m {MODULE_PATH} --step 2 --state-dir {shell_quote(state_dir)}"

    return format_step(body, next_cmd, title="Execution Planning")


# =============================================================================
# Step 2: Implementation
# =============================================================================


def format_step_2(qr: QRState, state_dir: str) -> str:
    """Wave-aware implementation dispatch."""
    if qr.state == LoopState.RETRY:
        title = "Implementation - Fix Mode"
        mode_script = get_mode_script_path("developer/exec_implement.py")
        invoke_cmd = f"uv run python -m {mode_script} --step 1 --state-dir {shell_quote(state_dir)}"
        actions = build_fix_mode_dispatch(
            banner_label="IMPLEMENTATION FIX",
            iteration=qr.iteration,
            fix_mode_line="FIX MODE: Code QR found issues.",
            constraint=ORCHESTRATOR_CONSTRAINT,
            agent_type="developer",
            invoke_cmd=invoke_cmd,
            follow_up=(
                "Developer reads QR report and fixes issues in <milestone> blocks.",
                "After developer completes, re-run Code QR for fresh verification.",
            ),
        )
    elif verify_has_failures(state_dir):
        # Reset code/doc QR so the post-verify fix gets a fresh review (the gate is
        # a pure renderer -- the reset side-effect lives here, in the step the gate
        # routes TO). After reset, qr-impl-code.json is clean -> this branch runs,
        # not RETRY. Fix the failing suite/lint/type; the full pipeline (code QR ->
        # docs -> doc QR -> verify) then re-runs to re-review the fix for new bugs.
        reset_qr_for_reverify(state_dir)
        # Intentionally thinner than the RETRY (code-QR) fix path: it carries the
        # failing-check detail inline but no mode-script scaffolding -- a red final
        # suite needs the failures, not per-milestone fix context.
        title = "Implementation - Verify Fix Mode"
        vf = load_verify_state(state_dir)
        detail = format_verify_failures_for_fix(vf) if vf else "  (verify state unavailable)"
        actions = [
            "FINAL VERIFICATION FAILED -- the full suite/lint/type is not green.",
            "",
            "Failing checks:",
            detail,
            "",
            ORCHESTRATOR_CONSTRAINT,
            "",
            "Dispatch a developer to FIX the failing checks above against the CURRENT",
            "code. Target ONLY the failure -- do NOT re-implement milestones that",
            "already pass.",
            "",
            "Each prompt must include:",
            "  - Plan file: $PLAN_FILE",
            "  - The failing check(s) and summary above",
            "  - The relevant files / code_intents from plan.json",
            "",
            "Run the affected tests to confirm the fix. The workflow then re-runs",
            "Code QR, Documentation, Doc QR, and Final Verification fresh, so a fix",
            "that introduces a new issue is caught.",
            "",
            "ERROR HANDLING (you NEVER fix code yourself):",
            "  Clear problem + solution: Task(developer) immediately",
            "  Difficult/unclear problem: Task(debugger) to diagnose first",
            f"  Uncertain how to proceed: {ESCALATE_HANDLER} with options",
        ]
    else:
        title = "Implementation"
        actions = [
            "Execute ALL milestones using wave-aware parallel dispatch.",
            "",
            "WAVE-AWARE EXECUTION:",
            "  - Milestones within same wave: dispatch in PARALLEL",
            "    (Multiple Task calls in single response)",
            "  - Waves execute SEQUENTIALLY",
            "    (Wait for wave N to complete before starting wave N+1)",
            "",
            "Use waves identified in step 1.",
            "",
            "DOC-ONLY MILESTONES: skip any milestone flagged is_documentation_only in",
            "plan.json -- it has no code_intents to implement. The Documentation phase",
            "(step 6) authors all docs (CLAUDE.md, README, inline comments) against the",
            "real code, so doc-only milestones need no developer dispatch here.",
            "",
            ORCHESTRATOR_CONSTRAINT,
            "",
            "FOR EACH WAVE:",
            "  1. Dispatch developer agents for ALL milestones in wave:",
            "     Task(developer): Milestone N",
            "     Task(developer): Milestone M  (if parallel)",
            "",
            "  2. Each prompt must include:",
            "     - Plan file: $PLAN_FILE",
            "     - Milestone: [number and name]",
            "     - Files: [exact paths to create/modify]",
            "     - Acceptance criteria: [from plan, milestones[].acceptance_criteria]",
            # Code Intent is the durable contract: there are no
            # plan-time diffs. The developer regenerates the implementation JIT
            # against the live file; impl-code QR reviews exactly what ships.
            "     - Code Intent: the milestone's code_intents[] from plan.json",
            "       ({file, function, behavior, decision_refs}) -- the durable contract.",
            "       Implement these behaviors just-in-time against the CURRENT file:",
            "       read the live file, then write code satisfying the Code Intent +",
            "       acceptance criteria. No precomputed diffs, nothing to re-anchor.",
            "",
            "  3. Wait for ALL agents in wave to complete",
            "",
            "  4. Run tests: pytest / tsc / go test -race",
            "     Pass criteria: 100% tests pass, zero warnings",
            "",
            "  5. Proceed to next wave (repeat 1-4)",
            "",
            "After ALL waves complete, proceed to Code QR.",
            "",
            "ERROR HANDLING (you NEVER fix code yourself):",
            "  Clear problem + solution: Task(developer) immediately",
            "  Difficult/unclear problem: Task(debugger) to diagnose first",
            f"  Uncertain how to proceed: {ESCALATE_HANDLER} with options",
        ]

    body = "\n".join(actions)
    next_cmd = f"uv run python -m {MODULE_PATH} --step 3 --state-dir {shell_quote(state_dir)}"

    return format_step(body, next_cmd, title=title)


# =============================================================================
# Steps 3, 7: QR Decompose
# =============================================================================


def format_qr_decompose(step: int, phase: str, state_dir: str, qr: QRState) -> str:
    """Dispatch QR decomposition agent for a phase."""
    config = get_phase_config(phase)
    decompose_script = config["decompose_script"]

    title = f"{PHASE_QR_NAME.get(phase, 'QR')} Decompose"

    # Skip if already decomposed
    if qr_file_exists(state_dir, phase):
        next_step = step + 1
        body = "\n".join(
            [
                f"QR items for {phase} already defined.",
                "Proceeding to verification of existing items.",
            ]
        )
        next_cmd = f"uv run python -m {MODULE_PATH} --step {next_step} --state-dir {shell_quote(state_dir)}"
        return format_step(body, next_cmd, title=f"{title} - Skipped")

    actions = build_qr_decompose_dispatch(
        decompose_script, phase, state_dir, qr.iteration, ORCHESTRATOR_CONSTRAINT
    )

    body = "\n".join(actions)
    next_step = step + 1
    next_cmd = f"uv run python -m {MODULE_PATH} --step {next_step} --state-dir {shell_quote(state_dir)}"

    return format_step(body, next_cmd, title=title)


# =============================================================================
# Steps 4, 8: QR Verify (parallel)
# =============================================================================


def format_qr_verify(
    step: int, phase: str, state_dir: str, qr: QRState, qr_state: dict | None = None
) -> str:
    """Dispatch parallel QR verification agents."""
    config = get_phase_config(phase)
    verify_script = config["verify_script"]

    title = f"{PHASE_QR_NAME.get(phase, 'QR')} Verify"

    items, _ = prepare_verify_items(state_dir, phase, qr, qr_state=qr_state)
    if items is None:
        decompose_step = step - 1
        body = f"Error: qr-{phase}.json not found or malformed. Routing back to decompose step."
        retry_cmd = (
            f"uv run python -m {MODULE_PATH} --step {decompose_step} --state-dir {shell_quote(state_dir)}"
        )
        return format_step(body, retry_cmd, title=title)
    if not items:
        next_step = step + 1
        body = "All items already verified. Proceeding with pass."
        # No agents dispatched → collapse to single NEXT STEP (pass) so the
        # prompt doesn't render "Count PASS vs FAIL" for zero agents.
        next_cmd = f"uv run python -m {MODULE_PATH} --step {next_step} --state-dir {shell_quote(state_dir)} --qr-status pass"
        return format_step(body, next_cmd=next_cmd, title=title)

    # Build the full verify action block (dispatch + PHASE 1/PHASE 2 aggregation
    # prose) -- shared with planner.py via build_qr_verify_dispatch, which owns the
    # cap scheme, vg-NNN labels, shell-quoting, the pinned Start: command, and the
    # aggregation prose. Only the constraint differs between orchestrators.
    actions = build_qr_verify_dispatch(
        verify_script, phase, state_dir, items, ORCHESTRATOR_CONSTRAINT
    )

    body = "\n".join(actions)
    next_step = step + 1
    base_cmd = f"uv run python -m {MODULE_PATH} --step {next_step} --state-dir {shell_quote(state_dir)}"

    return format_step(
        body,
        title=title,
        if_pass=f"{base_cmd} --qr-status pass",
        if_fail=f"{base_cmd} --qr-status fail",
    )


# =============================================================================
# Step 6: Documentation
# =============================================================================


def format_step_6(qr: QRState, state_dir: str) -> str:
    """Dispatch technical writer for documentation."""
    mode_script = get_mode_script_path("technical_writer/exec_docs.py")

    if qr.state == LoopState.RETRY:
        title = "Documentation - Fix Mode"
        invoke_cmd = f"uv run python -m {mode_script} --step 1 --state-dir {shell_quote(state_dir)}"
        # No follow-up prose here -- the doc retry ends at the dispatch (unlike code retry).
        actions = build_fix_mode_dispatch(
            banner_label="DOCUMENTATION FIX",
            iteration=qr.iteration,
            fix_mode_line="FIX MODE: Doc QR found issues.",
            constraint=ORCHESTRATOR_CONSTRAINT,
            agent_type="technical-writer",
            invoke_cmd=invoke_cmd,
        )
    else:
        title = "Documentation"
        actions = [
            ORCHESTRATOR_CONSTRAINT,
            "",
        ]

        invoke_cmd = f"uv run python -m {mode_script} --step 1 --state-dir {shell_quote(state_dir)}"
        actions.append(
            subagent_dispatch(
                agent_type="technical-writer",
                command=invoke_cmd,
            )
        )

    body = "\n".join(actions)
    next_cmd = f"uv run python -m {MODULE_PATH} --step 7 --state-dir {shell_quote(state_dir)}"

    return format_step(body, next_cmd, title=title)


# =============================================================================
# Step 10: Final Verification (run full suite/lint/type -> verify.json)
# =============================================================================


def format_step_10_verify(state_dir: str) -> str:
    """Final Verification work step: run the full suite/lint/type, record verify.json.

    Authored AFTER doc-QR because exec-docs (step 6) edits source comments/
    docstrings after the last code test -- without this step a doc-authoring break
    (or a latent red suite QR's judgement missed) ships to the retrospective.
    """
    record_cmd = pin_cwd(
        f"uv run python -m skills.planner.cli.verify --state-dir {shell_quote(state_dir)} \\"
    )
    actions = [
        "FINAL VERIFICATION",
        "",
        "Run the project's FULL test suite, linter, and type checker -- the",
        "canonical commands from the project's CLAUDE.md / conventions (e.g.",
        "pytest / tsc / go test -race; ruff / biome check; pyright / tsc). Run the",
        "WHOLE suite, not a subset, against the final code AND docs.",
        "",
        "Record all three results (paste each command's ACTUAL summary line):",
        "  " + record_cmd,
        "    --suite <pass|fail> --suite-summary '<test summary line>' \\",
        "    --lint <pass|fail> --lint-summary '<lint summary line>' \\",
        "    --type <pass|fail> --type-summary '<type summary line>'",
        "",
        "Record HONESTLY from the real output -- the gate finalizes on this file.",
        "All three checks are required in one invocation.",
    ]
    body = "\n".join(actions)
    next_cmd = f"uv run python -m {MODULE_PATH} --step 11 --state-dir {shell_quote(state_dir)}"
    return format_step(body, next_cmd, title="Final Verification")


# =============================================================================
# Step 11: Final Verification Gate (deterministic route on verify.json)
# =============================================================================


def _build_verify_iteration_escalation(state_dir: str, vf: "VerifyFile") -> str:
    """User escalation when Final Verification keeps failing at the ceiling.

    Mirrors the QR gate's iteration-limit escalation: the suite is unfixable in
    QR_ITERATION_LIMIT cycles, so hand control to the user rather than loop again.
    Rendered without format_step so it emits no "NEXT STEP" footer -- the user's
    choice selects the next command.
    """
    accept_cmd = (
        f"cd {shell_quote(str(SKILLS_DIR))} && "
        f"uv run python -m {MODULE_PATH} --step 12 --state-dir {shell_quote(state_dir)}"
    )
    title = "Final Verification Gate -- Iteration Limit Reached"
    detail_lines = [
        f"Still failing after {vf.iteration} verify cycles:",
        "",
        format_verify_failures_for_fix(vf),
    ]
    accept_block = [
        f"  Accept (finalize despite the red suite -- report it honestly):\n    {accept_cmd}",
        "  Abort: stop here. Do NOT invoke a next step; report the failures to the user.",
    ]
    return _render_iteration_limit_banner(
        title=title,
        limit_line=f"FINAL VERIFICATION REACHED THE ITERATION LIMIT ({QR_ITERATION_LIMIT}).",
        detail_lines=detail_lines,
        accept_block=accept_block,
        forbidden_third_item="Hiding or downgrading the unresolved failures",
    )


def format_step_11_verify_gate(state_dir: str) -> str:
    """Deterministic Final Verification gate routing on verify.json.

    Dedicated (NOT build_gate_output): a binary suite/lint/type record has no QR
    severity tiers, so the QR gate's de-escalation -- which would auto-pass a red
    suite at high iterations -- and its QR-specific escalation prose must not
    apply. Reuses format_step / QR_ITERATION_LIMIT / the format helpers only.
    """

    def _cmd(step: int) -> str:
        return f"uv run python -m {MODULE_PATH} --step {step} --state-dir {shell_quote(state_dir)}"

    title = "Final Verification Gate"
    vf = load_verify_state(state_dir)

    # Fail closed: a missing / unparseable / incomplete record is never a pass --
    # re-run the verify step (which overwrites verify.json), do NOT finalize or fix.
    if vf is None or not verify_is_complete(vf):
        parts = [
            format_gate_result(passed=False),
            "",
            "verify.json is missing or incomplete -- re-run Final Verification and",
            "record all three checks (suite, lint, type).",
        ]
        return format_step("\n".join(parts), _cmd(10), title=title)

    if not verify_failures(vf):
        parts = [
            format_gate_result(passed=True),
            "",
            "Full suite, lint, and types verified. Proceed to retrospective.",
            "",
            format_forbidden(
                "Asking the user whether to proceed - the workflow is deterministic",
                "Re-running verification - it already passed",
            ),
        ]
        return format_step("\n".join(parts), _cmd(12), title=title)

    # Failures recorded. Escalate at the ceiling instead of looping forever.
    if vf.iteration >= QR_ITERATION_LIMIT:
        return _build_verify_iteration_escalation(state_dir, vf)

    # Below the ceiling: route back to Implementation (step 2 renders verify-fix
    # mode with the QR reset side-effect). The gate is a pure renderer.
    parts = [
        format_gate_result(passed=False),
        "",
        "Final verification failed:",
        "",
        format_verify_failures_for_fix(vf),
        "",
        "Routing back to Implementation to fix. Code QR and Doc QR re-run fresh",
        "afterward so a fix that introduces a new bug is caught.",
        "",
        format_forbidden(
            "Finalizing or reporting COMPLETED with a red suite/lint/type",
            "Skipping the re-review after the fix",
            "Editing code yourself from this gate step",
        ),
    ]
    return format_step("\n".join(parts), _cmd(2), title=title)


# =============================================================================
# Step 12: Retrospective
# =============================================================================


def format_step_12(state_dir: str) -> str:
    """Present execution retrospective (terminal step)."""
    actions = [
        "PRESENT retrospective to user (do not write to file):",
        "",
        "EXECUTION RETROSPECTIVE",
        "=======================",
        "Plan: [path]",
        "Status: COMPLETED | BLOCKED | ABORTED",
        "",
        "Milestone Outcomes: | Milestone | Status | Notes |",
        "Reconciliation Summary: [if run]",
        "Plan Accuracy Issues: [if any]",
        "Deviations from Plan: [if any]",
        "Quality Review Summary: [counts by category]",
        "Feedback for Future Plans: [actionable suggestions]",
    ]
    # Defensive: step 12 is reachable only via a green gate or an explicit user
    # accept-at-ceiling. If verify.json is not all-green, surface the outstanding
    # failures so the retrospective cannot silently report COMPLETED over a red suite.
    vf = load_verify_state(state_dir)
    if vf is not None and verify_failures(vf):
        actions += [
            "",
            "OUTSTANDING VERIFICATION FAILURES (accepted at the ceiling -- report honestly):",
            format_verify_failures_for_fix(vf),
        ]
    body = "\n".join(actions)
    return format_step(body, title="Retrospective")


# =============================================================================
# Step Dispatch
# =============================================================================


def format_output(
    step: int,
    state_dir: str,
    qr_status: str | None,
    reconciliation_check: bool,
    plan: "Plan | None" = None,
    qr_states: dict | None = None,
) -> str:
    """Format output for display."""

    # Derive QR state from on-disk qr-{phase}.json (planner.py does the same).
    # Iteration lives in the state file; the gate no longer passes --qr-iteration
    # or --qr-fail (see skills/planner/shared/qr/cli.py), so CLI-derived fallbacks
    # would stale-cache iteration=1 forever in fix loops.
    # Applies to work step (N), decompose (N+1), verify (N+2), and gate (N+3) —
    # iteration bumps in verify only fire when state == RETRY, and the gate
    # renders different prose on retry. Restricting to N/N+1 would leave
    # verify/gate running as INITIAL even mid-fix-loop (Qodo review #4).
    phase = EXECUTOR_STEP_PHASES.get(step)
    qr_state, qr = resolve_qr_for_step(qr_states, state_dir, phase, qr_status)

    # Non-QR steps carry no QR phase (1; 10/11/12 use verify.json); 2 and 6 have
    # one the dispatch ignores. These early-return BEFORE the phase-is-None guard.
    if step == 1:
        return format_step_1(state_dir, reconciliation_check)
    elif step == 2:
        return format_step_2(qr, state_dir)
    elif step == 6:
        return format_step_6(qr, state_dir)
    elif step == 10:
        return format_step_10_verify(state_dir)
    elif step == 11:
        return format_step_11_verify_gate(state_dir)
    elif step == 12:
        return format_step_12(state_dir)

    # Steps 3-5 and 7-9 are QR steps; each has a phase in EXECUTOR_STEP_PHASES.
    invalid_step = f"Error: invalid step {step} (valid: 1-12)"
    if phase is None:
        return invalid_step

    if step in (3, 7):
        return format_qr_decompose(step, phase, state_dir, qr)
    if step in (4, 8):
        return format_qr_verify(step, phase, state_dir, qr, qr_state)
    if step in EXECUTOR_GATE_CONFIG:  # {5, 9}
        qr_name, work_step, pass_step, pass_message, fix_target = EXECUTOR_GATE_CONFIG[step]
        if not qr_status:
            return f"Error: --qr-status required for step {step} ({qr_name} Gate)"
        return build_gate_output(
            module_path=MODULE_PATH,
            qr_name=qr_name,
            qr=qr,
            step=step,
            work_step=work_step,
            pass_step=pass_step,
            pass_message=pass_message,
            fix_target=fix_target,
            state_dir=state_dir,
            phase=phase,
            qr_state=qr_state,
            plan=plan,
        ).output
    return invalid_step


def main():
    parser = argparse.ArgumentParser(
        description="Plan Executor - Execute approved plans (12-step workflow)",
        epilog="Steps: plan -> implement -> code QR (decompose/verify/gate) -> docs -> doc QR (decompose/verify/gate) -> final verification (run/gate) -> retrospective",
    )

    parser.add_argument("--step", type=int, required=True)
    parser.add_argument(
        "--state-dir", type=str, default=None, help="State directory path (created in step 1)"
    )
    add_qr_args(parser)
    parser.add_argument("--reconciliation-check", action="store_true")

    args = parser.parse_args()

    if args.step < 1 or args.step > 12:
        sys.exit("Error: step must be 1-12")

    # Create state_dir for step 1 if not provided
    state_dir = args.state_dir
    if args.step == 1 and not state_dir:
        try:
            state_dir = tempfile.mkdtemp(prefix="executor-")
        except OSError as e:
            sys.exit(
                f"Error: failed to create executor state directory (tempdir={tempfile.gettempdir()}): {e}"
            )
        plan_path = Path(state_dir) / "plan.json"
        try:
            from skills.planner.shared.schema import Overview, Plan

            plan_path.write_text(Plan(overview=Overview(problem="", approach="")).model_dump_json(indent=2))
        except OSError as e:
            sys.exit(f"Error: failed to write plan skeleton to {plan_path}: {e}")

    # Validate state_dir for steps 2+
    if args.step > 1 and not state_dir:
        sys.exit(f"Error: --state-dir required for step {args.step}")

    # Clear stale verify.json on step 1 (reused state-dir path) so the current
    # session's step 10/11 is the sole writer of the session's verdict.
    if args.step == 1 and state_dir:
        verify_path(state_dir).unlink(missing_ok=True)

    # plan is threaded into format_output so the QR gate (steps 5/9) reuses this
    # parse instead of re-reading plan.json. None for step 1 (no plan yet).
    plan = None
    qr_states = None

    # Validate the (LLM-authored) plan.json before running step 2+ -- mirrors
    # planner.py. The orchestrator hand-writes plan.json in step 1 from the plan;
    # catch a malformed or non-conforming write here instead of letting downstream
    # steps re-derive against a broken contract.
    if args.step > 1 and state_dir:
        from skills.planner.shared.schema import SchemaValidationError, validate_state

        try:
            plan, qr_states = validate_state(state_dir)
        except SchemaValidationError as e:
            sys.exit(f"Schema validation failed: {e}")

        # validate_state checks structure + refs only. Also enforce the durable
        # contract on the re-derived plan: every code milestone keeps its
        # code_intents and is covered by a wave. That is the structural invariant
        # the executor needs (validate_structural_executability), called directly on
        # the Plan validate_state already parsed -- no second read of plan.json, and
        # no borrowing the planner's "plan-design" phase name. Kept out of
        # validate_state because the planner saves partial plans mid-build, which
        # would not yet satisfy it. Fail CLOSED on a missing plan.json: a step>1 run
        # always expects the real plan, and validate_state returns None for an absent
        # file -- so without this guard the executor would dispatch an empty
        # implementation with no structural check at all.
        if plan is None:
            sys.exit(f"Error: plan.json not found in {state_dir} (required for step {args.step})")

        errors = plan.validate_structural_executability()
        if errors:
            sys.exit("Plan completeness failed: " + "; ".join(errors))

        # Structural backstop for the format_step_1 instruction above: nothing in the
        # executor ever reads rejected_alternatives/constraints/risks/diagram_graphs
        # (grepped, zero hits), so non-empty here is unambiguous evidence the
        # orchestrator hand-transcribed fields it was told to omit -- hand-retyping a
        # schema whose fields the transcriber never reads reliably drops required
        # fields and fails validation. A prompt instruction is advisory; this makes
        # the omission enforced, not requested. planning_context.decisions carries
        # one exception: a code_intent.decision_refs is the durable contract shown to developer
        # sub-agents, so decisions are allowed through when at least one code_intent
        # actually references one. Decisions present with no code_intent referencing
        # any of them is still the same hand-transcription mistake and stays rejected.
        pc = plan.planning_context
        has_decision_refs = any(
            ci.decision_refs for ms in plan.milestones for ci in ms.code_intents
        )
        if (
            (pc.decisions and not has_decision_refs)
            or pc.rejected_alternatives
            or pc.constraints
            or pc.risks
            or plan.diagram_graphs
        ):
            sys.exit(
                "Error: plan.json must not carry planning_context.rejected_alternatives, "
                "planning_context.constraints, planning_context.risks, or diagram_graphs -- "
                "the executor never reads them (see format_step_1's instructions). "
                "planning_context.decisions is permitted only when referenced by a "
                "code_intent.decision_refs; otherwise re-run step 1 and omit it too. "
                "overview/milestones/waves are all a conforming plan.json needs."
            )

    print(format_output(args.step, state_dir, args.qr_status, args.reconciliation_check, plan, qr_states))


if __name__ == "__main__":
    main()
