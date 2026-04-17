# incoherence/

Step-based incoherence detection workflow (22 steps) with detection, resolution, and application phases.

## Files

| File             | What                                                | When to read                                           |
| ---------------- | --------------------------------------------------- | ------------------------------------------------------ |
| `incoherence.py` | 22-step workflow, parent/sub-agent routing, CLI     | Debugging step behavior, changing phase assignments, adding dimensions |
| `__init__.py`    | Package marker                                      | Never (empty module)                                   |

## Run

```bash
uv run --project "${CLAUDE_PROJECT_DIR:-$HOME}/.claude/skills/scripts" python -m skills.incoherence.incoherence --step-number 1
uv run --project "${CLAUDE_PROJECT_DIR:-$HOME}/.claude/skills/scripts" python -m skills.incoherence.incoherence --step-number 2 --thoughts "dim=A, findings=..."
```
