"""Plain-text dispatch prompt building blocks.

Composition via f-strings. No XML, no AST.

Three dispatch patterns:
- subagent_dispatch: Single agent, optional prompt, mandatory invoke command
- template_dispatch: Parallel agents, template with $var substitution per agent
- roster_dispatch: Parallel agents, shared context + unique tasks, fixed command
"""

import shlex
from collections.abc import Sequence
from pathlib import Path
from string import Template

# ============================================================================
# CONFIGURATION
# ============================================================================

# subagent.py -> prompts -> workflow -> lib -> skills -> scripts
SKILLS_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent
_SKILLS_DIR_Q = shlex.quote(str(SKILLS_DIR))


# ============================================================================
# SHARED PROMPTS
# ============================================================================
# Building block templates used by multiple dispatch patterns

TASK_TOOL_INSTRUCTION = """\
ACTION: Use the Task tool to spawn this agent.

Task tool parameters:
  - subagent_type: {agent_type}
  - model: {model_param}
  - prompt: Include the task and invoke command below
  - run_in_background: NEVER set this. Always omit or set false.
    Background agents return full session transcripts via TaskOutput,
    flooding the orchestrator context window. Foreground agents return
    only the agent's final text response."""

SUB_AGENT_INVOKE = """\
MANDATORY INVOKE BLOCK - Copy VERBATIM into the agent's prompt:
================================================================================
FIRST ACTION REQUIRED: Run this command via Bash tool before any other response.
Do NOT skip. Do NOT produce analysis first. Execute this command IMMEDIATELY.

Command: cd {working_dir} && {cmd}
================================================================================
CRITICAL: Copy the block above EXACTLY. Do not paraphrase or summarize.
The subagent needs "FIRST ACTION REQUIRED" to know it must run the command."""

PARALLEL_CONSTRAINT = """\
PARALLEL EXECUTION (MANDATORY):
    You MUST dispatch ALL {count} agents in ONE assistant message.
    Send exactly {count} Task tool calls together.

    CORRECT:
        [ONE message with Task call 1, Task call 2, ... Task call N]

    WRONG:
        [Message with Task call 1] -> [wait] -> [Message with Task call 2]

    FORBIDDEN: Waiting for any agent before dispatching the next."""


# ============================================================================
# MESSAGE TEMPLATES
# ============================================================================

# --- SUBAGENT DISPATCH (Single Agent) ---------------------------------------

SUBAGENT_TEMPLATE = """\
DISPATCH SUB-AGENT
==================

{task_tool_block}

TASK FOR THE SUB-AGENT:
{task_section}

{invoke_block}

After the sub-agent returns, continue with the next workflow step."""

# --- TEMPLATE DISPATCH (Parallel, Variable Substitution) --------------------

TEMPLATE_DISPATCH_TEMPLATE = """\
DISPATCH {count} PARALLEL AGENTS
================================

{parallel_block}

For EACH agent below, use Task tool with:
  - subagent_type: {agent_type}
  - model: {model_display}
  - prompt: Task description + MANDATORY INVOKE BLOCK (copy exactly as shown)

PROMPT CONSTRUCTION RULES:
  - The MANDATORY INVOKE BLOCK must appear VERBATIM in each prompt
  - DO NOT reduce it to just "Working directory: X / Command: Y"
  - The subagent needs "FIRST ACTION REQUIRED" to execute the command

{instruction_section}AGENTS:
{agents_section}

After ALL {count} agents return, continue with the next workflow step."""

TEMPLATE_AGENT_ENTRY = """\
--- Agent {index} ---
Task: {prompt}

{invoke_block}"""

# --- ROSTER DISPATCH (Parallel, Unique Tasks) -------------------------------

ROSTER_DISPATCH_TEMPLATE = """\
DISPATCH {count} PARALLEL AGENTS
================================

{parallel_block}

For EACH agent below, use Task tool with:
  - subagent_type: {agent_type}
  - model: {model_display}
  - prompt: Shared context + agent's unique task + MANDATORY INVOKE BLOCK (copy exactly)

PROMPT CONSTRUCTION RULES:
  - The MANDATORY INVOKE BLOCK must appear VERBATIM in each prompt
  - DO NOT reduce it to just "Working directory: X / Command: Y"
  - The subagent needs "FIRST ACTION REQUIRED" to execute the command

{instruction_section}{shared_context_section}AGENTS:
{agents_section}

After ALL {count} agents return, continue with the next workflow step."""

ROSTER_AGENT_ENTRY = """\
--- Agent {index} ---
Unique Task: {task}

{invoke_block}"""


# ============================================================================
# MESSAGE BUILDERS
# ============================================================================

# --- Building block functions -----------------------------------------------


def task_tool_instruction(agent_type: str, model: str | None) -> str:
    """Tell main agent how to spawn sub-agent via Task tool."""
    model_param = model if model else "omit (use default)"
    return TASK_TOOL_INSTRUCTION.format(agent_type=agent_type, model_param=model_param)


def sub_agent_invoke(cmd: str) -> str:
    """Tell sub-agent what command to run after spawning.

    working_dir is shlex-quoted so `cd` stays safe if SKILLS_DIR contains
    whitespace or shell metacharacters. Callers are responsible for
    pre-quoting any user-controlled substitutions inside `cmd`.
    """
    return SUB_AGENT_INVOKE.format(working_dir=_SKILLS_DIR_Q, cmd=cmd)


def parallel_constraint(count: int) -> str:
    """Enforce MANDATORY_PARALLEL execution for multiple agents."""
    return PARALLEL_CONSTRAINT.format(count=count)


# --- Dispatch pattern functions ---------------------------------------------


def subagent_dispatch(
    agent_type: str,
    command: str,
    prompt: str = "",
    model: str | None = None,
) -> str:
    """Generate prompt for single sub-agent dispatch.

    Args:
        agent_type: Task tool subagent_type (e.g., "general-purpose", "Explore")
        command: Shell command sub-agent must run after spawning
        prompt: Optional task description for sub-agent
        model: Optional model override ("haiku", "sonnet", "opus")

    Returns:
        Complete dispatch prompt as plain text
    """
    task_section = prompt if prompt else "(No additional task - agent follows invoke command)"

    return SUBAGENT_TEMPLATE.format(
        task_tool_block=task_tool_instruction(agent_type, model),
        task_section=task_section,
        invoke_block=sub_agent_invoke(command),
    )


def expand_template_pairs(
    template: str, command: str, targets: Sequence[dict[str, str]]
) -> list[dict[str, str]]:
    """Substitute a prompt+command template for each target, with validation.

    Shared validate-and-substitute core of template_dispatch and
    dispatch_renderer._expand_template_targets -- keep both callers thin; do NOT
    reintroduce a second copy of this logic (the twin is what audit Issue 6 was).
    Returns one {"prompt", "command"} dict per target; empty targets -> [].

    Raises:
        ValueError: if the template/command references a $var that NO target
            declares -- a typo (e.g. $item_ids for $item_id) or an unescaped
            literal "$" (write an intended literal "$" as "$$"). Also raised when
            a $var that SOME target provides is absent from another target -- a
            per-target inconsistency that would silently emit a literal "$var".
    """
    if not targets:
        return []
    managed = set().union(*(set(t) for t in targets))
    # Template parsing and identifier extraction are loop-invariant (template and
    # command never change per target), so build them once instead of N parses.
    prompt_tmpl = Template(template)
    cmd_tmpl = Template(command)
    referenced = set(prompt_tmpl.get_identifiers()) | set(cmd_tmpl.get_identifiers())
    unmanaged = referenced - managed
    if unmanaged:
        raise ValueError(
            f"template references undeclared variable(s) {sorted(unmanaged)} that no "
            f"target provides; write an intended literal '$' as '$$'"
        )
    pairs = []
    for i, t in enumerate(targets):
        prompt = prompt_tmpl.safe_substitute(t)
        cmd = cmd_tmpl.safe_substitute(t)
        missing = (referenced & managed) - set(t)
        if missing:
            raise ValueError(
                f"Template variable(s) {sorted('$' + m for m in missing)} not "
                f"substituted in target {i}: managed by sibling targets but "
                f"absent here. Provided keys: {sorted(t.keys())}"
            )
        pairs.append({"prompt": prompt, "command": cmd})
    return pairs


def template_dispatch(
    agent_type: str,
    template: str,
    targets: list[dict[str, str]],
    command: str,
    model: str | None = None,
    instruction: str | None = None,
) -> str:
    """Generate prompt for parallel dispatch with variable substitution.

    Template and command use $var syntax. Variables are substituted per-target
    before the LLM sees the prompt (expansion happens here, not at runtime).

    Args:
        agent_type: Task tool subagent_type for all agents
        template: Prompt template with $var placeholders
        targets: List of dicts, each providing variable bindings for one agent
        command: Command template with $var placeholders
        model: Optional model override for all agents
        instruction: Optional instruction text

    Returns:
        Complete dispatch prompt with expanded agent entries

    Raises:
        ValueError: propagated from expand_template_pairs -- the template/command
            references a $var no target declares (a typo or an unescaped literal
            "$"; write a literal "$" as "$$"), or a $var some target provides is
            absent from another target.
    """
    expanded = expand_template_pairs(template, command, targets)

    count = len(expanded)
    model_display = model if model else "default (omit parameter)"
    instruction_section = f"NOTE: {instruction}\n\n" if instruction else ""

    agents_lines = []
    for i, e in enumerate(expanded, 1):
        agents_lines.append(
            TEMPLATE_AGENT_ENTRY.format(
                index=i,
                prompt=e["prompt"],
                invoke_block=sub_agent_invoke(e["command"]),
            )
        )

    return TEMPLATE_DISPATCH_TEMPLATE.format(
        count=count,
        agent_type=agent_type,
        model_display=model_display,
        parallel_block=parallel_constraint(count),
        instruction_section=instruction_section,
        agents_section="\n\n".join(agents_lines),
    )


def roster_dispatch(
    agent_type: str,
    agents: list[str],
    command: str,
    shared_context: str = "",
    model: str | None = None,
    instruction: str | None = None,
) -> str:
    """Generate prompt for parallel dispatch with unique tasks per agent.

    Each agent receives shared_context + their unique task + the fixed command.
    Use when agents have fundamentally different roles (MIMD pattern).

    Args:
        agent_type: Task tool subagent_type for all agents
        agents: List of unique task descriptions, one per agent
        command: Fixed command all agents run (same for all)
        shared_context: Optional context included in every agent's prompt
        model: Optional model override for all agents
        instruction: Optional instruction text

    Returns:
        Complete dispatch prompt with agent entries
    """
    count = len(agents)
    model_display = model if model else "default (omit parameter)"
    instruction_section = f"NOTE: {instruction}\n\n" if instruction else ""
    shared_context_section = (
        f"SHARED CONTEXT (include in every agent's prompt):\n{shared_context}\n\n"
        if shared_context
        else ""
    )

    agents_lines = []
    for i, task in enumerate(agents, 1):
        agents_lines.append(
            ROSTER_AGENT_ENTRY.format(
                index=i,
                task=task,
                invoke_block=sub_agent_invoke(command),
            )
        )

    return ROSTER_DISPATCH_TEMPLATE.format(
        count=count,
        agent_type=agent_type,
        model_display=model_display,
        parallel_block=parallel_constraint(count),
        instruction_section=instruction_section,
        shared_context_section=shared_context_section,
        agents_section="\n\n".join(agents_lines),
    )


__all__ = [
    "parallel_constraint",
    "roster_dispatch",
    "sub_agent_invoke",
    # Dispatch templates
    "subagent_dispatch",
    # Building blocks
    "task_tool_instruction",
    "template_dispatch",
]
