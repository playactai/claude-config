"""Plain-text prompt building blocks for workflows.

Prompts as strings composed via f-strings. No XML, no AST.
"""

# format_file_content provides file content embedding with 4-backtick fencing
from skills.lib.workflow.prompts.file import format_file_content

# format_step provides step assembly: body content + continuation directive
# pin_cwd makes a prose command cwd-independent (absolute cd into SKILLS_DIR)
from skills.lib.workflow.prompts.step import format_step, pin_cwd
from skills.lib.workflow.prompts.subagent import (
    parallel_constraint,
    roster_dispatch,
    sub_agent_invoke,
    # Dispatch templates
    subagent_dispatch,
    # Building blocks
    task_tool_instruction,
    template_dispatch,
)

__all__ = [
    # File content embedding
    "format_file_content",
    # Step assembly
    "format_step",
    "parallel_constraint",
    "pin_cwd",
    "roster_dispatch",
    "sub_agent_invoke",
    # Dispatch templates
    "subagent_dispatch",
    # Building blocks
    "task_tool_instruction",
    "template_dispatch",
]
