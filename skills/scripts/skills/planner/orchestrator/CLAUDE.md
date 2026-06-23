# orchestrator/

Main workflow orchestrators: `planner` (6-step plan creation with one QR block) and `executor` (12-step plan execution with parallel QR + a final verification gate).

## Files

| File           | What                                                                | When to read                                          |
| -------------- | ------------------------------------------------------------------- | ----------------------------------------------------- |
| `planner.py`   | 6-step plan workflow: init/verify + plan-design QR block → APPROVED | Adding planning phases, changing QR gate routing      |
| `executor.py`  | 12-step exec workflow: impl + code-QR + docs + docs-QR + final-verify (run + gate) | Debugging executor steps, changing fix-mode/verify-gate detection |
| `__init__.py`  | Package marker                                                      | Never (empty module)                                  |

## Run

```bash
uv run --project "${CLAUDE_PROJECT_DIR:-$HOME}/.claude/skills/scripts" python -m skills.planner.orchestrator.planner --step 1
uv run --project "${CLAUDE_PROJECT_DIR:-$HOME}/.claude/skills/scripts" python -m skills.planner.orchestrator.executor --step 1
```
