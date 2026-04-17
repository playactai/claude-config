# workflow/

Workflow orchestration framework: metadata types, discovery, XML AST, prompt helpers, and constants.

## Architecture

Skills use CLI-based step invocation. The runtime flow is:

```
main() -> format_output() -> print() -> LLM reads -> follows <invoke_after>
```

`Workflow` and `StepDef` are metadata containers for introspection and testing — execution happens through each skill's own CLI, not through a central engine.

## Files

| File              | What                                                            | When to read                                        |
| ----------------- | --------------------------------------------------------------- | --------------------------------------------------- |
| `README.md`       | Framework architecture, testing domain types, invariants        | Understanding the workflow model, adding new skills |
| `core.py`         | `Workflow`, `StepDef`, `Arg` (metadata containers)              | Defining a new skill, adding `_params` metadata     |
| `discovery.py`    | `discover_workflows()` via importlib scanning                   | Debugging registry, understanding pull-based discovery |
| `cli.py`          | `mode_main()` and step-output CLI helpers                       | Adding CLI args to a skill entry point              |
| `constants.py`    | Shared constants, QR constants re-exports                       | Adding shared constants                             |
| `types.py`        | Domain types: `AgentRole`, `Dispatch`, `BoundedInt`, `ChoiceSet`, `Constant`, `QuestionOption`, `ResourceProvider` | QR gates, sub-agent dispatch, test domain generation |
| `quality_docs.py` | Extract phase/mode-aware content from code-quality docs         | Parsing conventions files for phase-specific guidance |
| `__init__.py`     | Public API exports                                              | Importing workflow types                            |

## Subdirectories

| Directory     | What                                                       | When to read                   |
| ------------- | ---------------------------------------------------------- | ------------------------------ |
| `ast/`        | AST nodes, builder, renderer for XML step output           | XML generation for step output |
| `prompts/`    | Plain-text prompt builders (step, subagent dispatch, file) | Writing step formatters        |
| `formatters/` | Re-exports from `ast/` for backwards compat                | Use `ast/` directly instead    |

## Test

```bash
SCRIPTS="${CLAUDE_PROJECT_DIR:-$HOME}/.claude/skills/scripts"
uv run --project "$SCRIPTS" pytest "$SCRIPTS" -v
uv run --project "$SCRIPTS" pytest "$SCRIPTS" -k deepthink -v
```
