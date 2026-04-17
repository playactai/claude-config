"""Guard the invariant documented in skills/lib/workflow/prompts/README.md:38.

Every command string rendered to the LLM must be prefixed with `uv run`. The
modern-python@trailofbits plugin installs a PreToolUse:Bash hook that denies
any Bash invocation starting with bare `python` / `python3` / `pip`, so a bare
`python3 -m skills.X ...` emitted in step output gets blocked when the LLM
tries to run it.

Previous migration sweeps landed the correct form in entry-point docs but
missed f-string / XML-invoke sites in renderer code; this test catches the
pattern by grepping the skills source tree.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"

# Patterns that can only appear in rendered-output producers, not in prose:
# - f"python3 -m ..." / f'python3 -m ...'  — f-string next_cmd
# - "python3 -m ..." inside Python list or arg (list-join or literal)
# - cmd="python3 -m ..." / cmd='python3 -m ...'  — XML-invoke literal
# - f"  python3 -m ..." — f-string CLI-hint line (leading whitespace in quote)
# Docstring prose (`Called via: python3 -m {mod}`) is not quoted immediately
# before `python3`, so these patterns don't match it.
FORBIDDEN_PATTERNS = [
    re.compile(r'["\']python3? -m\b'),
    re.compile(r'["\']\s+python3? -m\b'),
    re.compile(r"invoke_after:\s*python3? -m\b"),
    re.compile(r"^Start:\s*python3? -m\b", re.MULTILINE),
]


def _iter_skill_sources() -> list[Path]:
    return sorted(p for p in SKILLS_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_bare_python_invocation_in_rendered_output() -> None:
    violations: list[str] = []
    for path in _iter_skill_sources():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pat in FORBIDDEN_PATTERNS:
                if pat.search(line):
                    rel = path.relative_to(SKILLS_ROOT.parent)
                    violations.append(f"{rel}:{lineno}: {line.strip()}")
                    break
    assert not violations, (
        "Bare `python3 -m` / `python -m` in rendered-output sites "
        "(see skills/lib/workflow/prompts/README.md:38 — must be `uv run python -m`; "
        "the modern-python PreToolUse hook blocks the bare form):\n  "
        + "\n  ".join(violations)
    )
