"""Shared QR fix-mode step machine for all three phases.

ONE 3-step control flow (load failures -> apply fixes -> validate) parameterized by
--phase, replacing the three near-identical *_qr_fix.py files. The shared step-1 load
(load_qr_state -> iteration -> failed-items), the assembly scaffold, the next-command
generation, and the PASS-return contract live once in fix_dispatch_step; per-phase
content lives in FIX_CONTENT. Like the decompose path this separates the dispatcher
(fix_dispatch_step, cf. decompose.dispatch_step) from the per-phase content
(FIX_CONTENT, cf. content.DECOMPOSE_CONTENT) -- but co-locates both in THIS module
rather than splitting them across two files, because the fix content is small and
self-contained (decompose's larger content lives in content.py).

Step-2/step-3 bodies are a list[str] when static, or a (state_dir) -> list[str] builder
when they need runtime composition (impl-docs's apply injects temporal.md; plan-design's
validate shell-quotes state_dir into its command). plan-design's apply body is likewise a
builder: it imports the architect batch-contract preambles at call time.
"""

from __future__ import annotations

from collections.abc import Callable

from skills.lib.conventions import get_convention
from skills.lib.workflow.prompts.step import pin_cwd
from skills.planner.shared.builders import shell_quote
from skills.planner.shared.constraints import format_state_banner
from skills.planner.shared.qr.phases import get_phase_config
from skills.planner.shared.qr.utils import (
    format_failed_items_for_fix,
    get_qr_iteration_from_state,
    load_qr_state,
)
from skills.planner.shared.resources import (
    get_context_path,
    render_context_file,
    validate_state_dir_requirement,
)

# Step 1 and 3 titles are shared; step 2's title is per-phase (FIX_CONTENT[*]["apply_title"]).
LOAD_TITLE = "Load QR Failures"
VALIDATE_TITLE = "Validate Fixes"

StepBody = list[str] | Callable[[str], list[str]]

# ============================================================================
# PER-PHASE STEP-2 (APPLY) AND STEP-3 (VALIDATE) BODIES
# ============================================================================

IMPL_CODE_APPLY: list[str] = [
    "APPLY targeted fixes to code.",
    "",
    "For each failed item, fix the identified issue:",
    "",
    "Acceptance criteria mismatch:",
    "  - Re-read the acceptance criteria from plan",
    "  - Modify code to match expected behavior",
    "",
    "Temporal contamination:",
    "  - Rewrite comments to remove change-relative language",
    "  - Use Edit tool on source files",
    "",
    "Structural issues:",
    "  - Extract functions if >50 lines",
    "  - Remove duplicate logic",
    "  - Add missing error handling",
    "",
    "CONSTRAINT: Fix ONLY the failing items. Don't refactor passing code.",
]

IMPL_CODE_VALIDATE: list[str] = [
    "VALIDATE your fixes before returning to orchestrator.",
    "",
    "Run tests:",
    "  pytest / tsc / go test -race",
    "",
    "SELF-CHECK each fixed item:",
    "  For each FAIL item you addressed:",
    "    - Does the fix address the specific finding?",
    "    - Does the fix pass tests?",
    "    - Does the fix introduce new issues?",
    "",
    "If tests fail or self-check fails:",
    "  - Apply additional fixes",
    "  - Re-run tests",
    "",
    "If all tests pass:",
    "  Your complete response must be exactly: PASS",
    "  Do not add summaries, explanations, or any other text.",
]


def _impl_docs_apply(state_dir: str) -> list[str]:
    """impl-docs apply body; injects the temporal-contamination convention at runtime.

    state_dir is unused (it only needs get_convention) but kept to satisfy the
    StepBody builder signature that _resolve_body calls; the sibling _plan_design_validate
    is the builder that actually consumes state_dir.
    """
    return [
        "APPLY targeted fixes to documentation.",
        "",
        "COMMON FIXES:",
        "",
        "CLAUDE.md format violations:",
        "  - Rewrite to tabular format",
        "  - Remove forbidden sections",
        "  - Shorten overview to one sentence",
        "",
        "IK proximity failures:",
        "  - Move knowledge to README.md in SAME directory as code",
        "  - Add inline comments at enforcement points",
        "  - Remove references to external doc/ directories",
        "",
        "Temporal contamination:",
        "  - Rewrite comments to remove change-relative language",
        "",
        "Missing README.md:",
        "  - Create README.md with IK content",
        "  - Follow self-contained principle",
        "",
        "TEMPORAL REFERENCE:",
        get_convention("temporal.md"),
        "",
        "CONSTRAINT: Fix ONLY the failing items. Don't refactor passing docs.",
    ]


IMPL_DOCS_VALIDATE: list[str] = [
    "VALIDATE your fixes before returning to orchestrator.",
    "",
    "SELF-CHECK each fixed item:",
    "  For each FAIL item you addressed:",
    "    - Does the fix address the specific finding?",
    "    - CLAUDE.md: Is it now tabular format?",
    "    - IK: Is it now adjacent to relevant code?",
    "    - Comments: Are they free of temporal contamination?",
    "",
    "If self-check fails:",
    "  - Apply additional fixes",
    "",
    "If all checks pass:",
    "  Your complete response must be exactly: PASS",
    "  Do not add summaries, explanations, or any other text.",
]

# pin_cwd only prefixes a fixed `cd SKILLS_DIR`, and these commands carry a literal
# $STATE_DIR placeholder (the agent substitutes it). The BATCH-MODE preamble (header +
# JSON-RPC shape + method catalog + underscore note) and the pipe lead-in are shared
# with plan_design_execute so the architect and QR-fix surfaces teach one batch
# contract; only this prompt's example array and fix-pattern prose stay local.
def _plan_design_apply(state_dir: str) -> list[str]:
    from skills.planner.architect.plan_design_execute import (
        _render_batch_mode_preamble,
        _render_batch_pipe_preamble,
    )

    return [
        "APPLY targeted fixes to plan.json using CLI commands.",
        "",
        "SINGLE COMMAND EXAMPLES:",
        "",
        "Missing decision_log entry:",
        "  "
        + pin_cwd(
            "uv run python -m skills.planner.cli.plan --state-dir $STATE_DIR set-decision \\"
        ),
        "    --decision '<what was decided>' \\",
        "    --reasoning '<premise -> implication -> conclusion>'",
        "",
        "Missing code_intent:",
        "  "
        + pin_cwd(
            "uv run python -m skills.planner.cli.plan --state-dir $STATE_DIR set-intent \\"
        ),
        "    --milestone <milestone-id> --file <path> \\",
        "    --behavior '<what to implement>' \\",
        "    --decision-refs '<DL-001,DL-002>'",
        "",
        "Updating existing intent (requires --version from current state):",
        "  "
        + pin_cwd(
            "uv run python -m skills.planner.cli.plan --state-dir $STATE_DIR set-intent \\"
        ),
        "    --id <intent-id> --version <current-version> \\",
        "    --behavior '<updated description>'",
        "",
        *_render_batch_mode_preamble(),
        "",
        *_render_batch_pipe_preamble(),
        "  [",
        '    {"method": "set-decision", "params": {"decision": "Use polling", "reasoning": "30% webhook failures"}, "id": 1},',
        '    {"method": "set-intent", "params": {"milestone": "M-001", "file": "src/a.py", "behavior": "Add handler", "decision_refs": "DL-001"}, "id": 2},',
        '    {"method": "set-intent", "params": {"id": "CI-M-001-001", "version": 1, "behavior": "Updated description"}, "id": 3}',
        "  ]",
        "",
        "COMMON FIX PATTERNS:",
        "",
        "Invalid decision_refs:",
        "  - If decision exists but ref is wrong: update the intent",
        "  - If decision is missing: add it first, then update ref",
        "",
        "Policy default without backing:",
        "  - Add decision_log entry explaining user confirmation",
        "  - Or use <needs_user_input> to get confirmation NOW",
        "",
        "CONSTRAINT: Fix ONLY the failing items. Don't refactor passing items.",
    ]


def _plan_design_validate(state_dir: str) -> list[str]:
    """plan-design validate body; shell-quotes state_dir into the validate command."""
    return [
        "VALIDATE your fixes before returning to orchestrator.",
        "",
        "Run structural validation:",
        f"  {pin_cwd(f'uv run python -m skills.planner.cli.plan --state-dir {shell_quote(state_dir)} validate --phase plan-design')}",
        "",
        "SELF-CHECK each fixed item:",
        "  For each FAIL item you addressed:",
        "    - Does the fix address the specific finding?",
        "    - Does the fix introduce new issues?",
        "    - Is the reasoning chain multi-step (not single assertion)?",
        "",
        "If validation fails or self-check fails:",
        "  - Apply additional fixes",
        "  - Re-run validation",
        "",
        "If validation passes:",
        "  Your complete response must be exactly: PASS",
        "  Do not add summaries, explanations, or any other text.",
    ]


# ============================================================================
# FIX CONTENT REGISTRY
# ============================================================================

# Per-phase content for the shared fix step machine. Keys must stay in sync with
# QR_PHASES / DECOMPOSE_CONTENT / VERIFIERS (enforced by phases.validate_phase_registries).
FIX_CONTENT: dict[str, dict] = {
    "plan-design": {
        "apply_title": "Apply Targeted Fixes",
        "banner_label": None,  # plan-design step 1 has no checkpoint banner
        "intro": "QR-COMPLETENESS found issues in the plan.",
        "change_target": "in plan.json",
        "load_context": True,
        "common_issues": None,
        "context_preservation": [
            "CONTEXT PRESERVATION:",
            "  - Do NOT remove valid decision_log entries",
            "  - Do NOT change milestones unnecessarily",
            "  - Focus ONLY on addressing the specific failures",
            "",
            "CONTEXT.JSON CONTRACT: READ-ONLY.",
            "  - context.json is owned by the orchestrator",
            "  - You MUST NOT write, modify, or append to context.json",
            "  - Your fixes go to plan.json -- never context.json",
        ],
        "apply": _plan_design_apply,
        "validate": _plan_design_validate,
    },
    "impl-code": {
        "apply_title": "Apply Code Fixes",
        "banner_label": "IMPLEMENTATION-FIX",
        "intro": "Code QR found issues in implemented code.",
        "change_target": "in the codebase",
        "load_context": False,
        "common_issues": [
            "COMMON ISSUE TYPES:",
            "  - Acceptance criteria mismatch",
            "  - Temporal contamination in comments",
            "  - Structural issues (god functions, duplicate logic)",
            "  - Missing error handling",
        ],
        "context_preservation": [
            "CONTEXT PRESERVATION:",
            "  - Do NOT refactor unrelated code",
            "  - Focus ONLY on addressing the specific failures",
        ],
        "apply": IMPL_CODE_APPLY,
        "validate": IMPL_CODE_VALIDATE,
    },
    "impl-docs": {
        "apply_title": "Apply Doc Fixes",
        "banner_label": "TW-POST-IMPL",
        "intro": "Doc QR found issues in documentation.",
        "change_target": "documentation",
        "load_context": False,
        "common_issues": [
            "COMMON ISSUE TYPES:",
            "  - CLAUDE.md format violations (prose instead of tabular)",
            "  - IK proximity failures (docs not adjacent to code)",
            "  - Temporal contamination in comments",
            "  - Missing README.md when IK present",
        ],
        "context_preservation": [
            "CONTEXT PRESERVATION:",
            "  - Do NOT remove valid documentation",
            "  - Focus ONLY on addressing the specific failures",
        ],
        "apply": _impl_docs_apply,
        "validate": IMPL_DOCS_VALIDATE,
    },
}


def get_fix_content(phase: str) -> dict:
    """Per-phase fix content. Raises ValueError on an unknown phase (matches
    get_decompose_content / get_verifier via get_phase_config)."""
    get_phase_config(phase)
    return FIX_CONTENT[phase]


def _resolve_body(body: StepBody, state_dir: str) -> list[str]:
    """A fix step body is a static list or a (state_dir) -> list builder."""
    return body(state_dir) if callable(body) else body


# ============================================================================
# SHARED STEP MACHINE
# ============================================================================


def fix_dispatch_step(
    step: int, phase: str, module_path: str, content: dict, state_dir: str
) -> dict:
    """Route a fix step to its shared handler, drawing per-phase text from `content`.

    Step 1 (shared load + scaffold) loads qr-{phase}.json once and assembles the
    load-failures prompt; steps 2/3 emit the per-phase apply/validate bodies. The
    PASS-return contract (step 3 ends with no next command) is shared.
    """
    def next_cmd(s: int) -> str:
        return (
            f"uv run python -m {module_path} --step {s} "
            f"--phase {phase} --state-dir {shell_quote(state_dir)}"
        )

    if step == 1:
        validate_state_dir_requirement(step, state_dir)

        # One read of qr-{phase}.json; iteration and failed items both derive from it.
        qr_state = load_qr_state(state_dir, phase)
        qr_iteration = get_qr_iteration_from_state(qr_state)
        failed_items_block = format_failed_items_for_fix(qr_state) if qr_state else ""

        actions: list[str] = []
        if content["banner_label"]:
            actions += [format_state_banner(content["banner_label"], qr_iteration, "fix"), ""]
        actions += [
            f"FIX MODE - QR Iteration {qr_iteration}",
            "",
            content["intro"],
            "",
            failed_items_block
            if failed_items_block
            else f"Read QR report from: STATE_DIR/qr-{phase}.json",
            "",
        ]
        if content["load_context"]:
            context_file = get_context_path(state_dir) if state_dir else None
            context_display = render_context_file(context_file) if context_file else ""
            actions += [
                "PLANNING CONTEXT (reference for semantic validation):",
                "",
                context_display,
                "",
            ]
        actions += [
            "For EACH failed item:",
            "  1. Read the 'finding' field to understand the issue",
            f"  2. Identify what {content['change_target']} needs to change",
            "  3. Note the fix approach for step 2",
            "",
        ]
        if content["common_issues"]:
            actions += [*content["common_issues"], ""]
        actions += content["context_preservation"]

        return {"title": LOAD_TITLE, "actions": actions, "next": next_cmd(2)}

    if step == 2:
        return {
            "title": content["apply_title"],
            "actions": _resolve_body(content["apply"], state_dir),
            "next": next_cmd(3),
        }

    if step == 3:
        return {
            "title": VALIDATE_TITLE,
            "actions": _resolve_body(content["validate"], state_dir),
            "next": "",
        }

    return {"error": f"Invalid step {step}"}
