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


def format_forbidden(*items: str) -> str:
    """Forbidden block. Dynamic args because each gate has different items."""
    lines = "\n".join(f"  - {item}" for item in items)
    return f"FORBIDDEN:\n{lines}"


def format_gate_result(passed: bool) -> str:
    """Gate result banner.

    WHY no iteration count: Prevents LLM from rationalizing "small enough"
    issues after multiple fix cycles. Only pass/fail state matters.
    """
    return "GATE RESULT: PASS" if passed else "GATE RESULT: FAIL"


def build_qr_verify_dispatch(verify_script: str, state_dir: str, items: list[dict]) -> tuple[str, int]:
    """Build the parallel QR-verify template_dispatch shared by planner + executor.

    Single owner of the verify fan-out shape: the balanced-group cap scheme, the
    display-only vg-NNN labels, shell-quoting of the --qr-item flags and the
    --state-dir, the checks_summary truncation, and the pinned "Start:" command.
    Returns (dispatch_text, group_count); each orchestrator appends its own
    PHASE 1/PHASE 2 aggregation prose (which legitimately differ).

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

    sd = shlex.quote(state_dir) if state_dir else "''"
    base_cmd = f"uv run python -m {verify_script} --step 1 --state-dir {sd} $qr_item_flags"
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
