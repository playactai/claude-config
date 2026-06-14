"""Shared string builders for planner output.

Constants for static text, functions for parameterized output.
"""

import shlex

THINKING_EFFICIENCY = (
    "THINKING EFFICIENCY:\n"
    "  Max 5 words per step. Symbolic notation preferred.\n"
    "  -> for implies | for alternatives ; for sequence\n"
    '  Example: "QR failed -> route step 8 | iteration++"'
)

PEDANTIC_ENFORCEMENT = (
    "QR exists to catch problems BEFORE they reach production.\n"
    "ALL issues must be fixed before proceeding."
)

SCRIPT_MODE_RULES = (
    "SCRIPT-MODE DISPATCH RULES:\n"
    "\n"
    "Your Task prompt contains ONLY the exact invoke command.\n"
    "\n"
    "FORBIDDEN in Task prompt:\n"
    "  - Task descriptions or summaries\n"
    "  - Goals or objectives\n"
    "  - Context from conversation\n"
    "  - Explanations of what the sub-agent should do\n"
    "  - Environment variables or STATE_DIR values\n"
    "\n"
    "The script tells the sub-agent what to do. You just invoke it."
)

# The QR-verify AGGREGATE-step forbidden items, shared by both orchestrators.
# Single source of truth: the planner and executor inline copies had drifted (the
# executor copy dropped the two plan-state lines), the exact silent divergence one
# SSOT removes. Both plan-state lines only restate the orchestrator's standing
# Read/Edit/Write prohibition, so this superset is behavior-safe in every phase.
QR_VERIFY_FORBIDDEN = (
    "Interpreting results beyond PASS/FAIL tallying",
    "Claiming 'diminishing returns' or 'comprehensive enough'",
    "Reading plan.json or any state files",
    "Writing, rendering, or summarizing the plan",
    "Skipping the next step command",
    "Proceeding to a later step without QR PASS",
)


def shell_quote(path: str | None) -> str:
    """Shell-quote a path for safe interpolation into an emitted command string.

    Prevents breakage on paths with spaces and blocks copy/paste shell injection
    via a metacharacter-bearing state_dir; an absent path renders as an explicit
    '' rather than a bare gap. Single owner for planner.py, executor.py, and
    gates.py, which had drifted -- executor/gates quoted, planner did not (audit
    §4: "planner interpolates state_dir ... without shlex.quote").
    """
    return shlex.quote(path) if path else "''"


def format_forbidden(*items: str) -> str:
    """Forbidden block. Dynamic args because each gate has different items."""
    lines = "\n".join(f"  - {item}" for item in items)
    return f"FORBIDDEN:\n{lines}"


def format_qr_verify_forbidden() -> str:
    """QR-verify AGGREGATE-step forbidden block (QR_VERIFY_FORBIDDEN superset).

    Both orchestrators call this so the list cannot drift again.
    """
    return format_forbidden(*QR_VERIFY_FORBIDDEN)


def format_gate_result(passed: bool) -> str:
    """Gate result banner.

    WHY no iteration count: Prevents LLM from rationalizing "small enough"
    issues after multiple fix cycles. Only pass/fail state matters.
    """
    return "GATE RESULT: PASS" if passed else "GATE RESULT: FAIL"


def build_qr_verify_dispatch(
    verify_script: str, phase: str, state_dir: str, items: list[dict]
) -> tuple[str, int]:
    """Build the parallel QR-verify template_dispatch shared by planner + executor.

    Single owner of the verify fan-out shape: the balanced-group cap scheme, the
    display-only vg-NNN labels, the injected --phase, shell-quoting of the
    --qr-item flags and the --state-dir, the checks_summary truncation, and the
    pinned "Start:" command. Returns (dispatch_text, group_count); each
    orchestrator appends its own PHASE 1/PHASE 2 aggregation prose (which
    legitimately differ).

    Extracted because the two inlined copies had already drifted -- the planner
    copy interpolated item ids and state_dir unquoted while the executor copy
    shlex-quoted both, an injection divergence in commands the agent may copy/run.
    """
    from skills.lib.workflow.prompts import pin_cwd, template_dispatch
    from skills.planner.shared.qr.constants import VERIFY_MAX_PARALLEL, VERIFY_TARGET_PER_GROUP
    from skills.planner.shared.qr.utils import balance_verify_groups

    balanced = balance_verify_groups(
        items, max_parallel=VERIFY_MAX_PARALLEL, target_per_group=VERIFY_TARGET_PER_GROUP
    )
    targets = [
        {
            "group_id": f"vg-{idx:03d}",
            "item_ids": ",".join(i["id"] for i in group_items),
            "qr_item_flags": " ".join(f"--qr-item {shlex.quote(i['id'])}" for i in group_items),
            "item_count": str(len(group_items)),
            "checks_summary": "; ".join(i.get("check", "")[:40] for i in group_items[:3]),
        }
        for idx, group_items in enumerate(balanced, 1)
    ]

    sd = shell_quote(state_dir)
    base_cmd = f"uv run python -m {verify_script} --step 1 --phase {shell_quote(phase)} --state-dir {sd} $qr_item_flags"
    # pin_cwd: the prose "Start:" line is a command the agent may copy and run
    # directly, so it carries the absolute cd the invoke block already has --
    # otherwise a drifted cwd yields "No module named 'skills'".
    tmpl = (
        "Verify QR group: $group_id ($item_count items)\n"
        "Items: $item_ids\n"
        "Checks: $checks_summary\n"
        "\n"
        "Start: " + pin_cwd(base_cmd)
    )
    dispatch_text = template_dispatch(
        agent_type="quality-reviewer",
        template=tmpl,
        targets=targets,
        command=base_cmd,
        instruction=f"Verify {len(balanced)} groups ({len(items)} items) in parallel.",
    )
    return dispatch_text, len(balanced)
