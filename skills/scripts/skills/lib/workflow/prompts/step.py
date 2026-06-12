"""Step assembly for workflow prompts.

format_step() is the sole assembler. invoke_after logic is internal.
"""

import shlex
from pathlib import Path

# SKILLS_DIR calculation matches subagent.py pattern: both modules are in
# skills/lib/workflow/prompts/, so .parent.parent.parent.parent.parent
# traverses: prompts/ -> workflow/ -> lib/ -> skills/ -> scripts/
SKILLS_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent
_SKILLS_DIR_Q = shlex.quote(str(SKILLS_DIR))


def pin_cwd(command: str) -> str:
    """Prefix a shell command with an absolute ``cd`` into SKILLS_DIR.

    The structured next-step and sub-agent-invoke paths already embed this
    prefix (see format_step below and prompts.subagent.sub_agent_invoke). Use
    this for commands that appear in PROSE an agent may copy and run directly:
    a bare ``uv run python -m skills...`` fails with "No module named 'skills'"
    when the agent's cwd has drifted (e.g. into a /tmp state dir) between Bash
    calls, because the Bash tool does not persist cwd. The absolute, shlex-quoted
    cd makes the invocation cwd-independent regardless of where the agent stands.
    """
    return f"cd {_SKILLS_DIR_Q} && {command}"


def format_step(
    body: str, next_cmd: str = "", title: str = "", if_pass: str = "", if_fail: str = ""
) -> str:
    """Assemble complete workflow step: title + body + invoke directive.

    Args:
        body: Free-form prompt content (no wrapper needed)
        next_cmd: Command for next step (empty string signals completion)
        title: Optional title rendered as "TITLE\\n======\\n\\n" header
        if_pass: Branching command when QR gate passes
        if_fail: Branching command when QR gate fails

    Returns:
        Complete step output as plain text

    Raises:
        ValueError: if exactly one of if_pass/if_fail is set, or if branching
            (if_pass/if_fail) is mixed with next_cmd. Both combinations would
            silently mis-render -- a lone if_pass falls through to "WORKFLOW
            COMPLETE", and branch+next_cmd silently drops next_cmd. Fail loud
            instead (mirrors InvokeAfterNode.__post_init__ validation).
    """
    if bool(if_pass) != bool(if_fail):
        raise ValueError(
            "format_step: if_pass and if_fail must be provided together (branching requires both)"
        )
    if (if_pass or if_fail) and next_cmd:
        raise ValueError(
            "format_step: branching (if_pass/if_fail) and next_cmd are mutually exclusive"
        )

    if title:
        header = f"{title}\n{'=' * len(title)}\n\n"
        body = header + body

    if if_pass and if_fail:
        # Branching invoke for QR gate routing: the LLM chooses based on
        # aggregated QR outcome (all pass vs any fail).
        # SKILLS_DIR is shlex-quoted so paths with spaces/metachars stay safe.
        invoke = (
            f"NEXT STEP (MANDATORY -- execute exactly one):\n"
            f"    Working directory: {SKILLS_DIR}\n"
            f"    ALL agents returned PASS  ->  cd {_SKILLS_DIR_Q} && {if_pass}\n"
            f"    ANY agent returned FAIL   ->  cd {_SKILLS_DIR_Q} && {if_fail}\n\n"
            f"This is a mechanical routing decision. Do not interpret, summarize, "
            f"or assess the results.\n"
            f"Count PASS vs FAIL, then execute the matching command."
        )
        return f"{body}\n\n{invoke}"

    elif next_cmd:
        # Working directory is explicit because CLI execution context varies.
        # Command is self-contained with cd prefix to avoid working directory issues.
        invoke = (
            f"NEXT STEP:\n"
            f"    Working directory: {SKILLS_DIR}\n"
            f"    Command: cd {_SKILLS_DIR_Q} && {next_cmd}\n\n"
            f"Execute this command now."
        )
        return f"{body}\n\n{invoke}"

    else:
        return f"{body}\n\nWORKFLOW COMPLETE - Return the output from the step above. Do not summarize."
