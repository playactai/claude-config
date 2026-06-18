#!/usr/bin/env python3
"""Impl docs execution - post-implementation documentation workflow.

6-step workflow for technical-writer sub-agent:
  1. Task Description (POST_IMPL type, deliverables)
  2. Extract Plan Information (IK, modified files, milestones)
  3. CLAUDE.md Index Format (tabular format rules)
  4. README.md Creation (creation criteria, IK mapping)
  5. Author Code Comments (inline comments + docstrings authored in real source)
  6. Output Format (documentation report)

This is the EXECUTE script for first-time post-impl documentation.
For QR fix mode, see exec_docs_qr_fix.py.
Router (exec_docs.py) dispatches to appropriate script.
"""

from skills.planner.shared.builders import shell_quote
from skills.planner.shared.constraints import format_state_banner
from skills.planner.shared.resources import validate_state_dir_requirement
from skills.planner.shared.schema import DOC_ONLY_DELIVERABLES_FILTER

STEPS = {
    1: "Task Description",
    2: "Extract Plan Information",
    3: "CLAUDE.md Index Format",
    4: "README.md Creation Criteria",
    5: "Author Code Comments",
    6: "Output Format",
}


def get_step_guidance(step: int, module_path: str | None = None, **kwargs) -> dict:
    """Return guidance for the given step."""
    MODULE_PATH = module_path or "skills.planner.technical_writer.exec_docs_execute"
    state_dir = kwargs.get("state_dir", "")
    validate_state_dir_requirement(step, state_dir or None)
    state_dir_arg = f" --state-dir {shell_quote(state_dir)}" if state_dir else ""

    if step == 1:
        banner = format_state_banner("TW-POST-IMPL", 1, "work")
        return {
            "title": STEPS[1],
            "actions": [
                banner,
                "",
                "TYPE: POST_IMPL",
                "",
                "TASK: Create documentation AFTER implementation is complete.",
                "",
                "You document what EXISTS. Implementation is done and stable.",
                "Code provided is correct and functional.",
                "",
                "PREREQUISITES:",
                "  - Plan file path (contains Invisible Knowledge, milestone descriptions)",
                "  - Implementation complete (all milestones executed)",
                "  - Quality review passed",
                "",
                "DELIVERABLES:",
                "  1. Inline comments & docstrings authored directly in the modified source",
                "     (sourced from the Decision Log + Invisible Knowledge -- the developer",
                "     adds none; you are the sole author of code documentation)",
                "  2. CLAUDE.md index entries for modified directories",
                "  3. README.md if Invisible Knowledge has content",
                "  4. Documentation-only milestone deliverables: author each",
                "     is_documentation_only milestone's files so its acceptance_criteria",
                "     are satisfied (planned docs that no developer produced)",
                "",
                "Read the plan file now to understand what was implemented.",
            ],
            "next": f"uv run python -m {MODULE_PATH} --step 2{state_dir_arg}",
        }

    elif step == 2:
        return {
            "title": STEPS[2],
            "actions": [
                "EXTRACT from plan file:",
                "",
                "1. INVISIBLE KNOWLEDGE section (if present):",
                "   - Architecture decisions not visible from code",
                "   - Tradeoffs made and why",
                "   - Invariants that must be maintained",
                "   - Assumptions underlying the design",
                "",
                "2. MODIFIED FILE LIST:",
                "   - From each milestone's ## Files section",
                "   - Group by directory for CLAUDE.md updates",
                "",
                "3. MILESTONE DESCRIPTIONS:",
                "   - What each milestone accomplished",
                "   - Use for WHAT column in CLAUDE.md index",
                "",
                "4. DECISION LOG (planning_context.decisions):",
                "   - DL-XXX entries: the WHY behind implementation choices",
                "   - Source for the inline WHY comments + docstring rationale you author",
                "",
                "5. DOCUMENTATION-ONLY MILESTONES (is_documentation_only == true):",
                "   - Their files[] are docs YOU author; their acceptance_criteria[] are",
                "     your authoring targets (write each file so every criterion holds):",
                f"       cat $STATE_DIR/plan.json | jq '{DOC_ONLY_DELIVERABLES_FILTER}'",
                "",
                "Write out your extraction before proceeding:",
                "  EXTRACTION:",
                "  - Invisible Knowledge: [summary or 'none']",
                "  - Modified directories: [list]",
                "  - Key changes: [per milestone]",
                "  - Decisions: [DL-XXX -> rationale]",
                "  - Doc-only deliverables: [file -> acceptance criteria, or 'none']",
            ],
            "next": f"uv run python -m {MODULE_PATH} --step 3{state_dir_arg}",
        }

    elif step == 3:
        return {
            "title": STEPS[3],
            "actions": [
                "UPDATE CLAUDE.md for each modified directory.",
                "",
                "FORMAT (tabular index):",
                "```markdown",
                "# CLAUDE.md",
                "",
                "## Overview",
                "",
                "[One sentence: what this directory contains]",
                "",
                "## Index",
                "",
                "| File         | Contents (WHAT)              | Read When (WHEN)                        |",
                "| ------------ | ---------------------------- | --------------------------------------- |",
                "| `handler.py` | Request handling, validation | Debugging request flow, adding endpoint |",
                "| `types.py`   | Data models, schemas         | Modifying data structures               |",
                "| `README.md`  | Architecture decisions       | Understanding system design             |",
                "```",
                "",
                "INDEX RULES:",
                "  - WHAT: Nouns and actions (handlers, validators, models)",
                "  - WHEN: Task-based triggers using action verbs",
                "  - Every file in directory should have an entry",
                "  - Exclude generated files (build artifacts, caches)",
                "",
                "IF CLAUDE.md exists but NOT tabular:",
                "  REWRITE completely (do not improve, replace)",
                "",
                "FORBIDDEN in CLAUDE.md:",
                "  - Explanatory prose (-> README.md)",
                "  - 'Key Invariants', 'Dependencies', 'Constraints' sections",
                "  - Overview longer than ONE sentence",
            ],
            "next": f"uv run python -m {MODULE_PATH} --step 4{state_dir_arg}",
        }

    elif step == 4:
        return {
            "title": STEPS[4],
            "actions": [
                "CREATE README.md ONLY if Invisible Knowledge has content.",
                "",
                "CREATION CRITERIA (create if ANY apply):",
                "  - Planning decisions from Decision Log",
                "  - Business context (why the product works this way)",
                "  - Architectural rationale (why this structure)",
                "  - Trade-offs made (what sacrificed for what)",
                "  - Invariants (rules not enforced by types)",
                "  - Historical context (why not alternatives)",
                "  - Performance characteristics (non-obvious)",
                "  - Non-obvious relationships between files",
                "",
                "DO NOT create README.md if:",
                "  - Directory is purely organizational",
                "  - All knowledge visible from reading source code",
                "  - You would only restate what code already shows",
                "",
                "SELF-CONTAINED PRINCIPLE:",
                "  README.md must be self-contained.",
                "  Do NOT reference external sources (wikis, doc/ directories).",
                "  Summarize external knowledge in README.md.",
                "",
                "CONTENT TEST for each sentence:",
                "  'Could a developer learn this by reading source files?'",
                "  If YES -> delete the sentence",
                "  If NO -> keep it",
                "",
                "DOCUMENTATION-ONLY MILESTONE DELIVERABLES:",
                "  For each is_documentation_only milestone (from step 2), create or update",
                "  every file in its files[] so each of its acceptance_criteria is satisfied.",
                "  These are explicit, planned documents -- distinct from the README criteria",
                "  above -- and you are their sole author.",
            ],
            "next": f"uv run python -m {MODULE_PATH} --step 5{state_dir_arg}",
        }

    elif step == 5:
        return {
            "title": STEPS[5],
            "actions": [
                "AUTHOR code comments & docstrings directly in the implemented source.",
                "",
                "You are the SOLE author of code documentation -- the developer added none.",
                "For each modified file, use the Edit tool on the REAL source file to add:",
                "  - Module comment: what the file contains (top of file)",
                "  - Docstrings: per public function/class -- what it does, when to use",
                "  - Inline WHY comments: reasoning behind non-obvious code",
                "",
                "SOURCE every WHY from the plan -- never invent:",
                "  - planning_context.decisions[] (DL-XXX): rationale for choices",
                "  - invisible_knowledge: invariants, tradeoffs, constraints",
                "  - intent-markers already in code (:PERF:, :UNSAFE:) per",
                "    conventions/intent-markers.md",
                "",
                "HYGIENE (conventions/temporal.md): timeless present tense. No change-",
                "relative language (Added, Changed, Replaced, Now, Previously) -- a comment",
                "must read correctly to someone seeing the code for the first time.",
                "",
                "WHY-not-WHAT: explain reasoning a reader cannot get from the code itself.",
                "If a line only restates the code, delete it.",
            ],
            "next": f"uv run python -m {MODULE_PATH} --step 6{state_dir_arg}",
        }

    elif step == 6:
        return {
            "title": STEPS[6],
            "actions": [
                "OUTPUT FORMAT:",
                "",
                "```",
                "Documented: [directory/] or [file:symbol]",
                "Type: POST_IMPL",
                "Tokens: [count]",
                "Index: [UPDATED | CREATED | VERIFIED]",
                "README: [CREATED | SKIPPED: reason]",
                "```",
                "",
                "Examples:",
                "",
                "```",
                "Documented: src/auth/",
                "Type: POST_IMPL",
                "Tokens: 180",
                "Index: UPDATED",
                "README: CREATED",
                "```",
                "",
                "```",
                "Documented: src/utils/",
                "Type: POST_IMPL",
                "Tokens: 95",
                "Index: CREATED",
                "README: SKIPPED: no invisible knowledge",
                "```",
                "",
                "If implementation unclear, add:",
                "  Missing: [what is needed]",
                "",
                "DO NOT include text before or after the format block.",
                "",
                "When complete, output: PASS",
            ],
            "next": "",
        }

    return {"error": f"Invalid step {step}"}


if __name__ == "__main__":
    from skills.lib.workflow.cli import mode_main

    mode_main(
        __file__,
        get_step_guidance,
        "Exec-Docs-Execute: Post-implementation documentation workflow",
        extra_args=[
            (["--state-dir"], {"type": str, "help": "State directory path"}),
        ],
    )
