# prompts/

Plain-text prompt building blocks for workflow output — step assembly, sub-agent dispatch templates, and file embedding.

## Files

| File           | What                                                            | When to read                                                        |
| -------------- | --------------------------------------------------------------- | ------------------------------------------------------------------- |
| `README.md`    | cd-wrapper invariant, invocation forms, SKILLS_DIR rationale    | Adding skill commands, understanding why emitted strings are bare   |
| `step.py`      | `format_step()` — title + body + NEXT STEP / branching invoke   | Adding a step formatter, changing cd-prefix convention              |
| `subagent.py`  | `subagent_dispatch`, `template_dispatch`, `roster_dispatch`     | Orchestrating sub-agents, adjusting parallel constraints            |
| `file.py`      | File content embedding helpers for prompts                      | Embedding reference files in step output                            |
| `__init__.py`  | Public API re-exports (`format_step`, dispatch builders)        | Importing prompt helpers                                            |
