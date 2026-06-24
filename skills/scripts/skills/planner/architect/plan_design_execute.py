#!/usr/bin/env python3
"""Plan design execution - first-time creation workflow.

6-step workflow for architect sub-agent:
  1. Task Analysis & Exploration Planning
  2. Codebase Exploration (inline: Glob, Grep, Read)
  3. Testing Strategy Discovery (may use question relay)
  4. Approach Generation
  5. Assumption Surfacing (may use question relay)
  6. Milestone Definition & Plan Writing

This is the EXECUTE script for first-time plan creation.
For QR fix mode, see quality_reviewer/exec_qr_fix.py (--phase plan-design).
Router (plan_design.py) dispatches to appropriate script.
"""

from skills.lib.workflow.constants import SUB_AGENT_QUESTION_FORMAT
from skills.lib.workflow.prompts.step import pin_cwd
from skills.planner.shared.builders import shell_quote
from skills.planner.shared.resources import (
    STATE_DIR_ARG_REQUIRED,
    PlannerResourceProvider,
    get_context_path,
    render_context_file,
    validate_state_dir_requirement,
)

STEPS = {
    1: "Task Analysis & Exploration Planning",
    2: "Codebase Exploration",
    3: "Testing Strategy Discovery",
    4: "Approach Generation",
    5: "Assumption Surfacing",
    6: "Milestone Definition & Plan Writing",
}


def _render_method_catalog() -> list[str]:
    """Render the plan CLI's RPC method catalog as prompt lines.

    Lists each method's full param-key set (underscore form) so the architect copies
    exact keys instead of inferring them from prose. Deliberately NOT the
    required/optional split: for the dual create/update commands the signature's
    required/optional split does not match create-vs-update requiredness, so showing it
    would mislabel them (the CREATE vs UPDATE note in step 6 carries that distinction).
    """
    # Local import: keeps prompt construction self-contained and avoids any module-load
    # cycle (cli.dispatch / cli.plan_commands do not import architect.*).
    from skills.planner.cli import plan_commands
    from skills.planner.cli.dispatch import discover_methods, list_methods

    catalog = list_methods(discover_methods(plan_commands))
    lines = ["", "RPC METHOD CATALOG -- exact param keys per method (underscores):"]
    for name in sorted(catalog):
        keys = sorted(set(catalog[name]["required"]) | set(catalog[name]["optional"]))
        lines.append(f"  {name:<19}{', '.join(keys) or '(none)'}")
    return lines


def get_step_guidance(step: int, module_path: str | None = None, **kwargs) -> dict:
    """Return guidance for the given step."""
    _provider = PlannerResourceProvider()
    MODULE_PATH = module_path or "skills.planner.architect.plan_design_execute"

    if step == 1:
        state_dir = kwargs.get("state_dir")
        validate_state_dir_requirement(step, state_dir)
        assert isinstance(state_dir, str)
        context_file = get_context_path(state_dir)
        context_display = render_context_file(context_file)

        return {
            "title": STEPS[1],
            "actions": [
                "PLANNING CONTEXT (from orchestrator):",
                "",
                context_display,
                "",
                SUB_AGENT_QUESTION_FORMAT,
                "",
                "TASK: Create implementation plan from user request.",
                "",
                "You will follow a 6-step workflow:",
                "  1. Task Analysis & Exploration Planning (current)",
                "  2. Codebase Exploration (inline: Glob, Grep, Read)",
                "  3. Testing Strategy Discovery (may ask user)",
                "  4. Approach Generation",
                "  5. Assumption Surfacing (may ask user)",
                "  6. Milestone Definition & Plan Writing",
                "",
                "If you need user input at any step, use <needs_user_input> XML.",
                "IMPORTANT: Save state to plan.json BEFORE yielding with <needs_user_input>.",
                "The orchestrator will relay the question and REINVOKE you fresh with the answer.",
                "When reinvoked, plan.json will contain your saved progress.",
                "",
                "STEP 1: TASK ANALYSIS",
                "",
                "Parse the user's task description. Identify:",
                "  - What needs to change (files, modules, behavior)",
                "  - What exploration is needed (patterns, constraints, existing code)",
                "  - What directories/files are relevant",
                "",
                "Read project context files to understand structure:",
                "  - Project root CLAUDE.md",
                "  - Subdirectory CLAUDE.md files in relevant areas",
                "  - All paths in context.json reference_docs field (if any)",
                "",
                "CONTEXT.JSON CONTRACT: READ-ONLY.",
                "  - context.json is owned by the orchestrator",
                "  - You MUST NOT write, modify, or append to context.json",
                "  - Your outputs go to plan.json (step 6) -- never context.json",
                "",
                "DO NOT write any files yet. Gather understanding for step 2.",
                "Record your analysis mentally for use in subsequent steps.",
            ],
            "next": f"uv run python -m {MODULE_PATH} --step 2 --state-dir {shell_quote(state_dir)}",
        }

    elif step == 2:
        state_dir = kwargs.get("state_dir", "")
        state_dir_arg = f" --state-dir {shell_quote(state_dir)}" if state_dir else ""
        return {
            "title": STEPS[2],
            "actions": [
                "STEP 2: CODEBASE EXPLORATION",
                "",
                "Use Glob, Grep, Read tools directly to discover:",
                "  - Existing patterns and implementations",
                "  - Constraints from code structure",
                "  - Conventions to follow",
                "",
                "Read conventions/ files as needed:",
                "  - structural.md (architectural patterns)",
                "  - temporal.md (comment hygiene)",
                "",
                "NUDGE: If you need additional context to plan well, read more files.",
                "Better to over-explore than under-explore.",
                "",
                "For any EXISTING function whose behavior you will modify or remove, grep the",
                "test suite NOW for every test exercising it (by name, by its callers, and by",
                "the changed behavior) -- you list all coupled tests per milestone in step 6.",
                "",
                "Record discoveries for use in steps 4-6. Do NOT write files.",
            ],
            "next": f"uv run python -m {MODULE_PATH} --step 3{state_dir_arg}",
        }

    elif step == 3:
        state_dir = kwargs.get("state_dir", "")
        state_dir_arg = f" --state-dir {shell_quote(state_dir)}" if state_dir else ""
        return {
            "title": STEPS[3],
            "actions": [
                "STEP 3: TESTING STRATEGY DISCOVERY",
                "",
                "DISCOVER testing strategy from:",
                "  - User conversation hints",
                "  - Project CLAUDE.md / README.md",
                "  - conventions/structural.md domain='testing-strategy'",
                "",
                "If testing approach is unclear, use <needs_user_input> to ask.",
                "",
                "Record confirmed strategy for use in step 6.",
                "Decisions will be recorded via CLI in step 6.",
            ],
            "next": f"uv run python -m {MODULE_PATH} --step 4{state_dir_arg}",
        }

    elif step == 4:
        state_dir = kwargs.get("state_dir", "")
        state_dir_arg = f" --state-dir {shell_quote(state_dir)}" if state_dir else ""
        return {
            "title": STEPS[4],
            "actions": [
                "STEP 4: APPROACH GENERATION",
                "",
                "GENERATE 2-3 approach options:",
                "  - Include 'minimal change' option",
                "  - Include 'idiomatic/modern' option",
                "  - Document advantage/disadvantage for each",
                "",
                "TARGET TECH RESEARCH (if new tech/migration):",
                "  - What is canonical usage of target tech?",
                "  - Does it have different abstractions?",
                "",
                "Use exploration findings from step 2 to ground tradeoffs.",
                "Record approach analysis for step 6.",
            ],
            "next": f"uv run python -m {MODULE_PATH} --step 5{state_dir_arg}",
        }

    elif step == 5:
        state_dir = kwargs.get("state_dir", "")
        state_dir_arg = f" --state-dir {shell_quote(state_dir)}" if state_dir else ""
        return {
            "title": STEPS[5],
            "actions": [
                "STEP 5: ASSUMPTION SURFACING",
                "",
                "FAST PATH: Skip if task involves NONE of:",
                "  - Migration to new tech",
                "  - Policy defaults (lifecycle, capacity, failure handling)",
                "  - Architectural decisions with multiple valid approaches",
                "",
                "FULL CHECK (if any apply):",
                "  Audit each category with OPEN questions:",
                "    Pattern preservation, Migration strategy, Idiomatic usage,",
                "    Abstraction boundary, Policy defaults",
                "",
                "  For each assumption needing confirmation:",
                "    Use <needs_user_input> BEFORE proceeding",
                "",
                "Record assumptions and user answers for step 6.",
            ],
            "next": f"uv run python -m {MODULE_PATH} --step 6{state_dir_arg}",
        }

    elif step == 6:
        plan_json_schema = _provider.get_resource("plan-json-schema.md")
        catalog_lines = _render_method_catalog()
        return {
            "title": STEPS[6],
            "actions": [
                "STEP 6: MILESTONE DEFINITION & PLAN WRITING (JSON-IR)",
                "",
                "JSON-IR ARCHITECTURE:",
                "  plan.json is AUTHORITATIVE until TW translates to Markdown.",
                "  Use CLI commands to build plan.json - DO NOT write JSON directly.",
                "",
                "EVALUATE approaches: P(success), failure mode, backtrack cost",
                "",
                "SELECT and record in Decision Log with MULTI-STEP chain:",
                "  BAD:  'Polling | Webhooks unreliable'",
                "  GOOD: 'Polling | 30% webhook failure -> need fallback anyway'",
                "",
                "CLI COMMANDS (single invocation syntax):",
                "",
                f"  {pin_cwd('uv run python -m skills.planner.cli.plan --state-dir $STATE_DIR <command>')}",
                "",
                "  Commands:",
                "    set-decision --decision '<what>' --reasoning '<premise->implication->conclusion>'",
                "    set-milestone --name '<name>' --files 'path/a.py,path/b.py'",
                "    set-intent --milestone M-001 --file path/a.py --behavior '<what>' --decision-refs 'DL-001'",
                "    set-wave --milestones 'M-001,M-002'",
                "",
                "BATCH MODE (preferred - reduces process invocations) -- pass JSON via stdin, never inline:",
                "",
                'JSON-RPC format: [{"method": "...", "params": {...}, "id": N}, ...]',
                *catalog_lines,
                "",
                "params use the EXACT underscore keys above; CLI flags are the same names",
                "hyphenated (--decision-refs <-> decision_refs). In batch params ALWAYS use",
                "underscores. Unknown keys are rejected.",
                "",
                "CREATE vs UPDATE (the catalog lists every key, not when each is required):",
                "  - CREATE (omit id): set-decision needs decision+reasoning; set-milestone",
                "    needs name; set-intent needs milestone+file+behavior; set-wave needs milestones.",
                "  - UPDATE (pass id + changed fields): set-intent still needs milestone (its parent);",
                "    set-wave still needs milestones (its new membership).",
                "  - version drives CAS on set-decision/set-milestone/set-intent; set-wave has no",
                "    version. version is rejected on create.",
                "  - set-diagram / add-diagram-* / list-* / init / validate are not create/update --",
                "    pass exactly the catalog keys.",
                "",
                "  # Write the batch JSON to a file (Write tool), then pipe it in:",
                f"  {pin_cwd('uv run python -m skills.planner.cli.plan --state-dir $STATE_DIR batch < /tmp/changes.json')}",
                "",
                "  # /tmp/changes.json (JSON escapes apostrophes/backslashes/newlines for you):",
                "  [",
                '    {"method": "set-decision", "params": {"decision": "Use polling", "reasoning": "30% webhook failures"}, "id": 1},',
                '    {"method": "set-milestone", "params": {"name": "Auth stack", "files": "src/auth.py"}, "id": 2},',
                '    {"method": "set-intent", "params": {"milestone": "M-001", "file": "src/auth.py", "behavior": "Add token validation", "decision_refs": "DL-001"}, "id": 3},',
                '    {"method": "set-wave", "params": {"milestones": "M-001"}, "id": 4},',
                '    {"method": "set-diagram", "params": {"type": "architecture", "scope": "overview", "title": "System Overview"}, "id": 5},',
                '    {"method": "add-diagram-node", "params": {"diagram": "DIAG-001", "node_id": "client", "label": "Client", "type": "service"}, "id": 6},',
                '    {"method": "add-diagram-node", "params": {"diagram": "DIAG-001", "node_id": "server", "label": "Server", "type": "service"}, "id": 7},',
                '    {"method": "add-diagram-edge", "params": {"diagram": "DIAG-001", "source": "client", "target": "server", "label": "calls", "protocol": "gRPC"}, "id": 8}',
                "  ]",
                "",
                'Response: [{"id": 1, "result": {"id": "DL-001", ...}}, ...]',
                'Errors: [{"id": N, "error": {"code": -32000, "message": "..."}}]',
                "",
                "DIAGRAM CREATION (if applicable):",
                "",
                "SKIP diagrams if:",
                "  - Pure refactoring (no new components)",
                "  - Single-file change",
                "  - Documentation-only milestone",
                "",
                "CREATE diagram if plan involves:",
                "  - Multiple services/components interacting",
                "  - Data flow through pipeline stages",
                "  - Protocol with state transitions",
                "  - SDK/API layer boundaries",
                "",
                "CLI WORKFLOW:",
                "",
                "1. Create diagram:",
                "   "
                + pin_cwd(
                    "uv run python -m skills.planner.cli.plan --state-dir $STATE_DIR set-diagram \\"
                ),
                "     --type architecture --scope overview --title 'System Overview'",
                "",
                "2. Add nodes (3-7 recommended, prevents visual overload):",
                "   "
                + pin_cwd(
                    "uv run python -m skills.planner.cli.plan --state-dir $STATE_DIR add-diagram-node \\"
                ),
                "     --diagram DIAG-001 --node-id client --label 'Client' --type service",
                "   "
                + pin_cwd(
                    "uv run python -m skills.planner.cli.plan --state-dir $STATE_DIR add-diagram-node \\"
                ),
                "     --diagram DIAG-001 --node-id server --label 'Server' --type service",
                "",
                "3. Add edges (label every edge):",
                "   "
                + pin_cwd(
                    "uv run python -m skills.planner.cli.plan --state-dir $STATE_DIR add-diagram-edge \\"
                ),
                "     --diagram DIAG-001 --source client --target server --label 'sends request' --protocol gRPC",
                "",
                "4. Render each diagram to fixed-width ASCII (<=80 cols) and store it so the",
                "   approved plan.md shows it. Write the ASCII to a file, then:",
                "   "
                + pin_cwd(
                    "uv run python -m skills.planner.cli.plan --state-dir $STATE_DIR set-diagram-render \\"
                ),
                "     --diagram DIAG-001 --content-file /tmp/diag-001.txt",
                "",
                "SCOPE VALUES:",
                "  - overview: Hero diagram, rendered after Overview section",
                "  - invisible_knowledge: Context for future LLM sessions",
                "  - milestone:M-XXX: Specific to what milestone implements",
                "",
                "NOTE: you build the graph IR (nodes/edges) AND render its ASCII. Validate",
                "      connectivity first (edges must reference real nodes), then lay out the ASCII.",
                "",
                "NOTE: plan.json skeleton already exists (created by orchestrator).",
                "      CLI commands ADD to it, do not need 'init'.",
                "",
                "MILESTONES (each deployable increment):",
                "  - Files: exact paths (each file in ONE milestone only)",
                "  - Requirements: specific behaviors",
                "  - Acceptance: testable pass/fail criteria",
                "  - Code Intent: the DURABLE CONTRACT (you read the source; there are no",
                "    diffs). Per file give symbol signatures + purpose, precise behavior",
                "    (control flow, error/edge handling, data shapes), the integration seam",
                "    by name, and a decision_ref for every value/threshold/tradeoff. The",
                "    developer implements it JIT against the live file at execution and",
                "    escalates if it is under-specified -- so make it complete.",
                "  - Tests: type, backing, scenarios. For any code_intent that modifies or",
                "    removes behavior in an EXISTING function, SWEEP the suite for ALL tests",
                "    exercising it (by function name, by its callers, and by the behavior",
                "    changed) and list EVERY coupled test class in tests -- not just the",
                "    obvious one; a sibling test on the same behavior left out is the miss",
                "    this prevents.",
                "  - Documentation-only milestone (pure docs, no code): create it with",
                "    'set-milestone ... --documentation-only' and give it NO code_intents;",
                "    exec-docs authors its docs at execution.",
                "",
                "PARALLELIZATION:",
                "  Vertical slices (parallel) > Horizontal layers (sequential)",
                "  BAD: M1=models, M2=services, M3=controllers (sequential)",
                "  GOOD: M1=auth stack, M2=users stack, M3=posts stack (parallel)",
                "  If file overlap: extract to M0 (foundation) or consolidate",
                "",
                "EXECUTION WAVES (author AFTER milestones, with set-wave):",
                "  Group milestones that run concurrently into one wave; sequence",
                "  dependent work across waves (W-001, W-002, ... run in order).",
                "  HARD RULE: never put two milestones that share a file in the same",
                "    wave -- they run as concurrent developer agents and would corrupt",
                "    that file mid-write. Sequence them across waves, or extract the",
                "    shared file to a foundation milestone in an earlier wave.",
                "  Every code milestone goes in EXACTLY ONE wave; documentation-only",
                "    milestones go in NO wave (exec-docs handles them). validate --phase",
                "    plan-design rejects file overlap and missing/duplicate coverage.",
                "",
                "VALIDATION: After building plan.json, run:",
                f"  {pin_cwd('uv run python -m skills.planner.cli.plan --state-dir $STATE_DIR validate --phase plan-design')}",
                "",
                "REFERENCE SCHEMA:",
                "",
                plan_json_schema,
                "",
                "When plan.json written and validation passes, output: PASS",
            ],
            "next": "",
        }

    return {"error": f"Invalid step {step}"}


if __name__ == "__main__":
    from skills.lib.workflow.cli import mode_main

    mode_main(
        __file__,
        get_step_guidance,
        "Plan-Design-Execute: Architect planning workflow",
        extra_args=[STATE_DIR_ARG_REQUIRED],
    )
