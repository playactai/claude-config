# orchestrator/

Main workflow orchestrators: `planner` (14-step plan creation with parallel QR) and `executor` (10-step plan execution with parallel QR).

## Files

| File           | What                                                            | When to read                                          |
| -------------- | --------------------------------------------------------------- | ----------------------------------------------------- |
| `planner.py`   | 14-step plan workflow: init/verify + design/code/docs QR blocks | Adding planning phases, changing QR gate routing     |
| `executor.py`  | 10-step exec workflow: impl + code-QR + docs + docs-QR + retro  | Debugging executor steps, changing fix-mode detection |
| `__init__.py`  | Package marker                                                  | Never (empty module)                                  |

Each orchestrator runs QR in a 4-step block per phase: `work → decompose → verify(N, parallel) → gate`.

## Run

```bash
uv run --project "${CLAUDE_PROJECT_DIR:-$HOME}/.claude/skills/scripts" python -m skills.planner.orchestrator.planner --step 1
uv run --project "${CLAUDE_PROJECT_DIR:-$HOME}/.claude/skills/scripts" python -m skills.planner.orchestrator.executor --step 1
```
