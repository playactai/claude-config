#!/usr/bin/env python3
"""
Interactive Sequential Planner - Orchestrator with parallel QR verification.

6-step planning workflow per INTENT.md:

Flow:
  1. plan-init (orchestrator captures context categories)
  2. context-verify (orchestrator self-checks handover completeness)
  3. plan-design-work (architect: Code Intent contract + decisions + diagram IR/render)
  4. plan-design-qr-decompose -> 5. verify(N) -> 6. route -> Plan Approved

Code Intent is the durable contract: at execution the developer regenerates the
implementation just-in-time per wave against the live file (see executor.py), and
impl-code QR is the single authoritative code review. There are no plan-time diffs.

QR Block Pattern (4 steps per phase):
  N   work        1 agent (architect)          Modified plan.json
  N+1 decompose   1 agent (QR)                  qr-{phase}.json
  N+2 verify      N agents (parallel, expanded) Each: PASS or FAIL
  N+3 route       0 agents (orchestrator)       Loop to N or approve plan
"""

import argparse
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from skills.lib.workflow.constants import (
    QUESTION_RELAY_HANDLER,
    SUB_AGENT_QUESTION_FORMAT,
)
from skills.lib.workflow.prompts import subagent_dispatch
from skills.lib.workflow.prompts.step import format_step
from skills.lib.workflow.types import AgentRole
from skills.planner.shared.builders import (
    THINKING_EFFICIENCY,
    build_qr_verify_dispatch,
    format_qr_verify_forbidden,
    shell_quote,
)
from skills.planner.shared.constraints import (
    ORCHESTRATOR_CONSTRAINT_EXTENDED,
    format_state_banner,
)
from skills.planner.shared.gates import GateResult, build_gate_output
from skills.planner.shared.qr.cli import add_qr_args
from skills.planner.shared.qr.types import LoopState, QRState, QRStatus
from skills.planner.shared.qr.utils import increment_qr_iteration, qr_file_exists
from skills.planner.shared.resources import get_mode_script_path

MODULE_PATH = "skills.planner.orchestrator.planner"


def _translate_plan(state_dir: str) -> str | None:
    """Mechanical translation: plan.json -> plan.md.

    Returns path to plan.md on success, None on failure.

    Why non-fatal: plan.json is the source of truth (the IR).
    plan.md is a convenience rendering. If translation fails,
    the workflow should still complete -- QR already approved
    the plan.json content.
    """

    from skills.planner.cli.plan_commands import PlanContext, _translate

    try:
        plan_md = str(Path(state_dir) / "plan.md")
        ctx = PlanContext(state_dir=Path(state_dir))
        _translate(ctx, plan_md)
        return plan_md
    except Exception as e:
        import sys

        print(f"Warning: plan.md translation failed: {e}", file=sys.stderr)
        return None


def _slugify(text: str) -> str:
    """Convert text to URL-safe slug.

    Lowercase, replace non-alphanumeric with hyphens, collapse consecutive
    hyphens, strip leading/trailing hyphens. Truncate to 60 characters.
    Fallback to "plan" when input reduces to empty string.

    ASCII-only: pattern [^a-z0-9] treats non-ASCII characters (e.g., Unicode
    letters) as non-alphanumeric and replaces them with hyphens.

    Returns slug suitable for YYYY-MM-DD-{slug}.md filename.
    """
    import re

    slug = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    # rstrip after truncation — if [:60] lands on a hyphen, strip the trailing
    # hyphen so filenames don't become "YYYY-MM-DD-...-.md".
    slug = slug[:60].rstrip("-") if slug else "plan"
    return slug or "plan"


def _find_repo_root() -> "Path | None":
    """Walk up from current file to find .git directory or file.

    Supports both regular repos (.git directory) and git worktrees
    (.git file pointing to parent repo).

    Returns repo root path on success, None if not found.
    """
    current = Path(__file__).resolve().parent
    while current != current.parent:
        git_path = current / ".git"
        if git_path.is_dir() or git_path.is_file():
            return current
        current = current.parent
    return None


def _save_plan_to_docs(state_dir: str) -> "Path | None":
    """Copy plan.md to docs/plans/YYYY-MM-DD-slug.md.

    Derives slug from plan.json overview.problem, finds repo root,
    creates docs/plans/ if needed. Appends numeric suffix (-2, -3, ...)
    if target file already exists.

    Returns output path on success, None on failure.
    Non-fatal: prints warning to stderr on failure.
    """
    import json

    try:
        state_path = Path(state_dir)
        plan_json = state_path / "plan.json"
        plan_md = state_path / "plan.md"

        if not plan_md.exists():
            print("Warning: plan.md not found in state_dir", file=sys.stderr)
            return None

        plan_data = json.loads(plan_json.read_text(encoding="utf-8"))
        problem = plan_data.get("overview", {}).get("problem", "")
        slug = _slugify(problem)

        repo_root = _find_repo_root()
        if not repo_root:
            print("Warning: repo root not found (.git directory)", file=sys.stderr)
            return None

        docs_plans = repo_root / "docs" / "plans"
        docs_plans.mkdir(parents=True, exist_ok=True)

        date_prefix = datetime.now().strftime("%Y-%m-%d")
        base_name = f"{date_prefix}-{slug}.md"
        target = docs_plans / base_name

        counter = 2
        while target.exists():
            target = docs_plans / f"{date_prefix}-{slug}-{counter}.md"
            counter += 1

        target.write_text(plan_md.read_text(encoding="utf-8"), encoding="utf-8")
        return target
    except Exception as e:
        print(f"Warning: failed to save plan to docs/plans/: {e}", file=sys.stderr)
        return None


QUESTION_RELAY_INSTRUCTION = SUB_AGENT_QUESTION_FORMAT


def _build_fix_mode_output(title, agent, agent_role, script, mode_total_steps, qr, ctx):
    """Build output for execute step in fix mode."""
    state_dir = ctx["state_dir"]

    action_children = []

    action_children.append(format_state_banner("PLAN-FIX", qr.iteration, "fix"))
    action_children.append("")
    action_children.append("FIX MODE: QR found issues.")
    action_children.append("")

    action_children.append(ORCHESTRATOR_CONSTRAINT_EXTENDED)
    action_children.append("")

    mode_script = get_mode_script_path(script)
    invoke_cmd = f"uv run python -m {mode_script} --step 1 --state-dir {shell_quote(state_dir)}"

    dispatch_prompt = subagent_dispatch(
        agent_type=agent,
        command=invoke_cmd,
    )
    action_children.append(dispatch_prompt)
    action_children.append("")
    action_children.append(f"{agent.title()} reads QR report and fixes issues.")
    action_children.append("After fixes complete, re-run QR for fresh verification.")

    next_step = ctx["step"] + 1
    next_cmd = f"uv run python -m {MODULE_PATH} --step {next_step} --state-dir {shell_quote(state_dir)}"

    return {
        "title": f"{title} - Fix Mode",
        "actions": action_children,
        "next": next_cmd,
    }


# =============================================================================
# Step Pattern Functions
# =============================================================================


def init_step(title, actions):
    """Step 1: creates state_dir, writes plan.json skeleton."""

    def handler(ctx):
        import json

        state_dir = tempfile.mkdtemp(prefix="planner-")

        plan_skeleton = {
            "overview": {"problem": "", "approach": ""},
            "planning_context": {
                "decisions": [],
                "rejected_alternatives": [],
                "constraints": [],
                "risks": [],
            },
            "invisible_knowledge": {
                "system": "",
                "invariants": [],
                "tradeoffs": [],
            },
            "milestones": [],
            "waves": [],
        }
        plan_path = Path(state_dir) / "plan.json"
        plan_path.write_text(json.dumps(plan_skeleton, indent=2))

        print(f"STATE_DIR={state_dir}")

        return {
            "title": title,
            "actions": actions,
            "next": f"uv run python -m {MODULE_PATH} --step 2 --state-dir {shell_quote(state_dir)}",
        }

    return handler


def verify_step(title, actions):
    """Step 2: context verification."""

    def handler(ctx):
        from skills.planner.shared.resources import validate_state_dir_requirement

        state_dir = ctx["state_dir"]

        validate_state_dir_requirement(2, state_dir)

        return {
            "title": title,
            "actions": actions,
            "next": f"uv run python -m {MODULE_PATH} --step 3 --state-dir {shell_quote(state_dir)}",
        }

    return handler


def execute_dispatch_step(
    title, agent, agent_role, script, mode_total_steps, post_dispatch=None, phase=None
):
    """Steps 3, 7, 11: work execution dispatch."""

    def handler(ctx):
        from skills.planner.shared.resources import validate_state_dir_requirement

        state_dir = ctx["state_dir"]
        qr = ctx["qr"]
        step = ctx["step"]

        validate_state_dir_requirement(step, state_dir)

        if qr.state == LoopState.RETRY:
            return _build_fix_mode_output(
                title, agent, agent_role, script, mode_total_steps, qr, ctx
            )

        action_children = []

        action_children.append(ORCHESTRATOR_CONSTRAINT_EXTENDED)
        action_children.append("")

        mode_script = get_mode_script_path(script)
        invoke_cmd = f"uv run python -m {mode_script} --step 1 --state-dir {shell_quote(state_dir)}"

        dispatch_prompt = subagent_dispatch(
            agent_type=agent,
            command=invoke_cmd,
        )
        action_children.append(dispatch_prompt)
        action_children.append("")

        if post_dispatch:
            action_children.extend(post_dispatch)

        next_step = step + 1
        next_cmd = f"uv run python -m {MODULE_PATH} --step {next_step} --state-dir {shell_quote(state_dir)}"

        return {
            "title": title,
            "actions": action_children,
            "next": next_cmd,
        }

    handler.phase = phase
    return handler


def qr_decompose_step(title, phase, script, model=None):
    """Steps 4, 8, 12: QR decomposition dispatch.

    Dispatches single QR agent to decompose artifact into verification items.
    Agent outputs qr-{phase}.json.

    Decompose runs exactly once per phase. If qr-{phase}.json already exists,
    decomposition is skipped and flow proceeds directly to verify step.
    """

    def handler(ctx):
        state_dir = ctx["state_dir"]
        qr = ctx["qr"]
        step = ctx["step"]

        if qr_file_exists(state_dir, phase):
            verify_step = step + 1
            return {
                "title": f"{title} - Skipped (items already defined)",
                "actions": [
                    f"QR items for {phase} already defined.",
                    "Proceeding to verification of existing items.",
                ],
                "next": f"uv run python -m {MODULE_PATH} --step {verify_step} --state-dir {shell_quote(state_dir)}",
            }

        action_children = []

        qr_name = f"QR-{phase.upper()}-DECOMPOSE"
        action_children.append(format_state_banner(qr_name, qr.iteration, "decompose"))
        action_children.append("")

        action_children.append(ORCHESTRATOR_CONSTRAINT_EXTENDED)
        action_children.append("")

        mode_script = get_mode_script_path(script)
        invoke_cmd = (
            f"uv run python -m {mode_script} --step 1 --phase {phase} "
            f"--state-dir {shell_quote(state_dir)}"
        )

        dispatch_prompt = subagent_dispatch(
            agent_type="quality-reviewer",
            command=invoke_cmd,
            model=model,
        )
        action_children.append(dispatch_prompt)
        action_children.append("")

        action_children.append("Expected output: qr-{phase}.json written to STATE_DIR")
        action_children.append("Orchestrator generates verification dispatch from this file.")

        next_step = step + 1
        next_cmd = f"uv run python -m {MODULE_PATH} --step {next_step} --state-dir {shell_quote(state_dir)}"

        return {
            "title": title,
            "actions": action_children,
            "next": next_cmd,
        }

    handler.phase = phase
    return handler


def qr_verify_step(title, phase):
    """Steps 5, 9, 13: Parallel QR verification with group-aware dispatch.

    Reads qr-{phase}.json and generates expanded dispatch.
    Decompose agent outputs item data. Orchestrator transforms this data
    into template_dispatch format. LLM sees ready-to-execute agent
    blocks, not substitution instructions.

    Uses repeated --qr-item flags (argparse action="append") instead of
    comma-separated --qr-items to avoid parsing ambiguity.
    """

    def handler(ctx):
        from skills.planner.shared.qr.phases import get_phase_config
        from skills.planner.shared.qr.utils import (
            by_blocking_severity,
            by_status,
            load_qr_state,
            query_items,
        )

        state_dir = ctx["state_dir"]
        step = ctx["step"]
        qr = ctx["qr"]

        qr_state = load_qr_state(state_dir, phase)
        if not qr_state or "items" not in qr_state:
            return {"error": f"qr-{phase}.json not found or malformed in {state_dir}"}

        # Capture the incremented iteration (executor.py does the same). Reading
        # qr_state["iteration"] after the increment would use the stale
        # pre-increment value loaded above, lagging de-escalation by one
        # iteration.
        iteration = (qr_state.get("iteration") or 1)
        if qr.state == LoopState.RETRY:
            new_iter = increment_qr_iteration(state_dir, phase)
            if new_iter is not None:
                iteration = new_iter

        # Dispatch only items at blocking severity for current iteration.
        items = query_items(qr_state, by_status("TODO", "FAIL"), by_blocking_severity(iteration))
        if not items:
            next_step = step + 1
            return {
                "title": title,
                "actions": ["All items already verified. Proceeding with pass."],
                "if_pass": f"uv run python -m {MODULE_PATH} --step {next_step} --state-dir {shell_quote(state_dir)} --qr-status pass",
                "if_fail": f"uv run python -m {MODULE_PATH} --step {next_step} --state-dir {shell_quote(state_dir)} --qr-status pass",
            }

        config = get_phase_config(phase)
        verify_script = config["verify_script"]

        # Re-bin items into balanced, capped parallel groups and build the dispatch
        # (shared with executor.py via build_qr_verify_dispatch -- one owner for the
        # cap scheme, vg-NNN labels, shell-quoting, and the pinned Start: command).
        dispatch_text, group_count = build_qr_verify_dispatch(verify_script, phase, state_dir, items)

        action_children = [
            ORCHESTRATOR_CONSTRAINT_EXTENDED,
            "",
            "=== PHASE 1: DISPATCH (delegate to sub-agents) ===",
            "",
            f"VERIFY: {len(items)} items",
            "",
            dispatch_text,
            "",
            "=== PHASE 2: AGGREGATE (your action after all agents return) ===",
            "",
            f"After ALL {group_count} agents return, tally results mechanically:",
            "  ALL agents returned PASS  ->  invoke next step with --qr-status pass",
            "  ANY agent returned FAIL   ->  invoke next step with --qr-status fail",
            "",
            format_qr_verify_forbidden(),
        ]

        next_step = step + 1
        base_cmd = f"uv run python -m {MODULE_PATH} --step {next_step} --state-dir {shell_quote(state_dir)}"

        return {
            "title": title,
            "actions": action_children,
            "if_pass": f"{base_cmd} --qr-status pass",
            "if_fail": f"{base_cmd} --qr-status fail",
        }

    handler.phase = phase
    return handler


def qr_route_step(title, phase, work_step, pass_step, pass_message, fix_target=None):
    """Step 6: Route based on aggregated QR results.

    PASS: delete qr file, approve the plan (pass_step=None -> terminal pass)
    FAIL: loop to work_step (fix mode detected via qr-{phase}.json inspection)
    """

    def handler(ctx):
        qr = ctx["qr"]
        state_dir = ctx.get("state_dir", "")
        step = ctx["step"]

        return build_gate_output(
            module_path=MODULE_PATH,
            qr_name=title,
            qr=qr,
            step=step,
            work_step=work_step,
            pass_step=pass_step,
            pass_message=pass_message,
            fix_target=fix_target,
            state_dir=state_dir,
            phase=phase,
            accept_findings=ctx.get("accept_findings", False),
            qr_state=ctx["qr_state"],
            plan=ctx.get("plan"),
        )

    handler.phase = phase
    return handler


# =============================================================================
# Step Definitions (1-6)
# =============================================================================

STEPS = {
    1: init_step(
        title="plan-init",
        actions=[
            "CONTEXT CAPTURE: Structure these categories from conversation:",
            "",
            "1. TASK_SPEC: what the plan is ABOUT, not how to write the plan (orchestration)",
            "   - SUBJECT: the user's underlying goal (what they want to accomplish)",
            "   - EXCLUDE: output instructions ('write to file X', 'create a plan for')",
            "   - CORRECT: 'OAuth-based authorization for the REST API'",
            "   - WRONG: 'Write plan to plans/foo-plan.md'",
            "   - Then: scope (directories/modules), out-of-scope",
            "2. CONSTRAINTS: MUST/SHOULD/MUST-NOT with sources -- or 'none confirmed'",
            "3. ENTRY_POINTS: file:function + why relevant -- or 'greenfield'",
            "4. REJECTED_ALTERNATIVES: what dismissed + why -- or 'none discussed'",
            "5. CURRENT_UNDERSTANDING: how system works; for bugs: symptom + reproduction",
            "6. ASSUMPTIONS: unverified inferences with confidence H/M/L -- or 'none'",
            "7. INVISIBLE_KNOWLEDGE: design rationale, invariants, accepted tradeoffs",
            "8. REFERENCE_DOCS: paths to project docs sub-agents should read (doc/*.md, specs/*) -- or 'none'",
            "",
            "FORMAT: High signal-to-noise. File refs over content. No ASCII diagrams.",
            "",
            "Mentally organize this context; you will write it to context.json in step 2.",
        ],
    ),
    2: verify_step(
        title="context-verify",
        actions=[
            "CONTEXT PERSISTENCE: Write context to STATE_DIR/context.json",
            "",
            "JSON SCHEMA:",
            "{",
            '  "task_spec": ["subject (not orchestration)", "scope: dir/module", "out-of-scope: X"],',
            '  "constraints": ["MUST: X", "SHOULD: Y"] or ["none confirmed"],',
            '  "entry_points": ["file:function - why relevant"] or ["greenfield"],',
            '  "rejected_alternatives": ["alternative - why dismissed"] or ["none discussed"],',
            '  "current_understanding": ["how system works", "bug: symptom + repro"],',
            '  "assumptions": ["inference (H/M/L confidence)"] or ["none"],',
            '  "invisible_knowledge": ["design rationale", "invariants", "tradeoffs"],',
            '  "reference_docs": ["doc/spec.md - what it specifies"] or ["none"]',
            "}",
            "",
            "ACTION: Use Write tool to create STATE_DIR/context.json with populated values.",
            "",
            "SELF-VERIFICATION (all must pass before proceeding to step 3):",
            "[ ] 1. Subject (what plan is ABOUT) statable in one sentence",
            "[ ] 2. At least one out-of-scope item explicit",
            "[ ] 3. At least one constraint OR explicit 'none confirmed'",
            "[ ] 4. Entry points identified OR 'greenfield'",
            "[ ] 5. Someone unfamiliar would understand why we're building this",
            "[ ] 6. Reference documentation paths captured or explicit 'none'",
            "",
            "IF ANY CHECK FAILS: gather missing context via AskUserQuestion or exploration.",
        ],
    ),
    # Plan-design phase (steps 3-6)
    3: execute_dispatch_step(
        title="plan-design-work",
        agent="architect",
        agent_role="architect",
        script="architect/plan_design.py",
        mode_total_steps=6,
        phase="plan-design",
        post_dispatch=[
            QUESTION_RELAY_HANDLER,
        ],
    ),
    4: qr_decompose_step(
        title="plan-design-qr-decompose",
        phase="plan-design",
        script="quality_reviewer/qr_decompose.py",
        model="opus",
    ),
    5: qr_verify_step(
        title="plan-design-qr-verify",
        phase="plan-design",
    ),
    6: qr_route_step(
        title="plan-design-qr-route",
        phase="plan-design",
        work_step=3,
        pass_step=None,
        pass_message="PLAN APPROVED. Ready for execution.",
        fix_target=AgentRole.ARCHITECT,
    ),
}


def get_step_guidance(
    step: int, qr_status, state_dir, accept_findings=False, plan=None, qr_states=None
) -> dict | str:
    """Returns guidance for a step.

    Iteration and fix mode derived from qr-{phase}.json file state.
    Phase derived from handler attribute set by step factory.
    accept_findings is the user's ceiling override, consumed only by the route step.
    plan is the validate_state parse threaded from main, reused by the route gate.
    qr_states is the validate_state qr dict threaded from main so the gate
    path avoids a second load_qr_state.
    """
    from skills.planner.shared.qr.utils import (
        get_qr_iteration_from_state,
        has_qr_failures_from_state,
        load_qr_state,
    )

    handler = STEPS.get(step)
    if not handler:
        return {"error": f"Invalid step {step}"}

    # Phase stored as handler attribute by step factory.
    # None for non-QR steps (1, 2).
    phase = getattr(handler, "phase", None)
    if qr_states is not None:
        qr_state = qr_states.get(phase) if state_dir and phase else None
    else:
        qr_state = load_qr_state(state_dir, phase) if state_dir and phase else None
    iteration = get_qr_iteration_from_state(qr_state) if qr_state else 1

    status = QRStatus(qr_status) if qr_status else None
    is_fix_mode = bool(qr_state and has_qr_failures_from_state(qr_state))
    state = LoopState.RETRY if is_fix_mode else LoopState.INITIAL
    qr = QRState(iteration=iteration, state=state, status=status)

    ctx = {
        "step": step,
        "qr": qr,
        "state_dir": state_dir,
        "accept_findings": accept_findings,
        "qr_state": qr_state,
        "plan": plan,
    }

    return handler(ctx)


def format_output(
    step: int, qr_status, state_dir, accept_findings=False, plan=None, qr_states=None
) -> str | GateResult:
    """Format output for display."""
    guidance = get_step_guidance(
        step, qr_status, state_dir=state_dir, accept_findings=accept_findings, plan=plan, qr_states=qr_states
    )

    if isinstance(guidance, GateResult):
        return guidance
    if isinstance(guidance, str):
        return guidance
    if "error" in guidance:
        return f"Error: {guidance['error']}"

    body_parts = []
    if step == 1:
        body_parts.append(THINKING_EFFICIENCY)
        body_parts.append("")

    for action in guidance["actions"]:
        body_parts.append(str(action))

    body = "\n".join(body_parts)
    title = guidance["title"]

    if_pass = guidance.get("if_pass")
    if_fail = guidance.get("if_fail")
    next_cmd = guidance.get("next", "")

    if if_pass and if_fail:
        return format_step(body, title=title, if_pass=if_pass, if_fail=if_fail)
    return format_step(body, next_cmd, title=title)


def main():
    """CLI entry point for planner orchestration."""
    parser = argparse.ArgumentParser(
        description="Interactive Sequential Planner (6-step orchestration workflow)",
        epilog="Step 1: init | Step 2: context-verify | Steps 3-6: plan-design work + QR",
    )

    parser.add_argument("--step", type=int, required=True)
    parser.add_argument(
        "--state-dir", type=str, default=None, help="State directory path (for retry mode)"
    )
    add_qr_args(parser)
    parser.add_argument(
        "--accept-findings",
        action="store_true",
        help="User override at the QR iteration ceiling: approve the plan as-is and finalize "
        "(render plan.md + save to docs/plans/) despite unresolved findings",
    )

    args = parser.parse_args()

    from skills.planner.shared.constants import PLANNER_GATE_STEPS, PLANNER_TOTAL_STEPS

    if args.step < 1:
        sys.exit("Error: step must be >= 1")

    # Validate state before running step (skip for step 1 which creates state).
    # Capture the parse so the route gate reuses it instead of re-reading plan.json.
    plan = None
    qr_states = None
    if args.step > 1 and args.state_dir:
        from skills.planner.shared.schema import SchemaValidationError, validate_state

        try:
            plan, qr_states = validate_state(args.state_dir)
        except SchemaValidationError as e:
            sys.exit(f"Schema validation failed: {e}")

    # Route steps require --qr-status; provide helpful output if missing
    if args.step in PLANNER_GATE_STEPS and not args.qr_status:
        gate_names = {6: "plan-design-qr-route"}
        print(f"PLANNER - Step {args.step}/{PLANNER_TOTAL_STEPS}: {gate_names[args.step]}")
        print()
        print("This is a route step. Re-invoke with --qr-status pass or --qr-status fail")
        print("based on the aggregated QR output from the previous step.")
        sys.exit(0)

    result = format_output(
        args.step,
        args.qr_status,
        state_dir=args.state_dir,
        accept_findings=args.accept_findings,
        plan=plan,
        qr_states=qr_states,
    )

    if isinstance(result, GateResult):
        # Why translate on terminal_pass: plan.json is the IR (modified by
        # QR fix cycles). plan.md is a rendered view. Terminal gate approval
        # signals plan.json is stable -- safe to regenerate the markdown.
        print(result.output)
        if result.terminal_pass and args.state_dir:
            plan_path = _translate_plan(args.state_dir)
            if plan_path:
                print(f"\nPlan rendered to: {plan_path}")
                docs_path = _save_plan_to_docs(args.state_dir)
                if docs_path:
                    print(f"Plan saved to: {docs_path}")
                else:
                    print("Copy plan.md from state_dir to desired location.")
    else:
        print(result)


if __name__ == "__main__":
    main()
