# prompts/

## Overview

`format_step()` is the sole assembler of step output. Every skill's `format_output()` funnels through it, so the cd-prefix wrapper it emits is a repository-wide invariant: skill authors can write `next_cmd = "uv run python -m skills.X"` without tracking cwd or project path themselves, because the wrapper prepends `cd '<SKILLS_DIR>' && …` to both the simple and the branching-invoke forms.

## The cd-Prefix Invariant

`format_step()` emits `NEXT STEP` blocks shaped like:

```
NEXT STEP:
    Working directory: <SKILLS_DIR>
    Command: cd '<SKILLS_DIR>' && <next_cmd>

Execute this command now.
```

Two consequences follow:

1. **Any `next_cmd` string built outside `format_step()` must replicate the cd prefix itself.** The orchestrator (`step.py`) is the only place where the invariant is guaranteed. A new dispatch path or a custom formatter that skips `format_step()` inherits responsibility for cwd.
2. **Do not double-wrap.** Adding `cd …` or `uv run --project <path>` inside the Python string produces commands like `cd X && cd X && uv run --project Y …` that either no-op or override the wrapper unexpectedly. The bare `uv run python -m skills.X` form is deliberate.

## SKILLS_DIR Resolution

`SKILLS_DIR` is computed at import time as `Path(__file__).resolve().parent.parent.parent.parent.parent`. The five-level traversal walks `prompts/ -> workflow/ -> lib/ -> skills/ -> scripts/` to land at the pyproject root. The path is `shlex.quote`d before emission so spaces or shell metacharacters in user home paths survive the `cd`.

This traversal count is brittle: moving `step.py` (or any intermediate package) up or down one level silently changes the resolved dir, and skills will `cd` into the wrong place at runtime. `subagent.py` uses the same count for the same reason — both files must move together, and the count must be re-verified after any restructure.

## Three Invocation Forms

The repository uses three distinct forms because three different callers evaluate the command string:

| Form                                                                                              | Used in                                                     | Why                                                                                                                                 |
| ------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `<invoke working-dir=".claude/skills/scripts" cmd="uv run python -m skills.X" />`                 | `SKILL.md` entry points, rendered parallel-dispatch XML     | Claude Code resolves `working-dir` against the active `.claude/` dir — user-global (`~/.claude/`) or project-local (`<repo>/.claude/`). `uv run` auto-discovers the pyproject from that cwd. One form covers both install layouts. |
| `uv run --project "${CLAUDE_PROJECT_DIR:-$HOME}/.claude/skills/scripts" python -m skills.X`       | Raw bash blocks in `INTENT.md`, SKIP-invoked `## Run` blocks | Plain bash doesn't get `working-dir` resolution. The env-var fallback points at the project-local install when Claude Code sets `CLAUDE_PROJECT_DIR`, otherwise at the user-global location. |
| `uv run python -m skills.X`                                                                       | Python `next_cmd` strings fed to `format_step()`            | The `cd '<SKILLS_DIR>' && …` wrapper handles cwd; the command just needs uv's env activation. Adding `--project` here would hardcode the install path the wrapper already resolved. |

When constructing commands for a new caller context, pick the form whose caller evaluates the string — if the caller gets Claude Code `<invoke>` resolution, use form 1; if it runs in plain bash with no wrapper, form 2; if it passes through `format_step()`, form 3.
