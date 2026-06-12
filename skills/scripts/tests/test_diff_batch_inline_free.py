"""Invariant guard: no plan-phase script emits unsafe inline shell forms.

Unsafe forms that break on apostrophes/backslashes in real code:
  --diff $'...'    (ANSI-C quoting of diff body)
  batch '['        (single-quoted JSON passed inline)

The plan-design scripts (the only plan-phase work/fix scripts after the
rigid-diff redesign removed the plan-code and plan-docs phases) must be free
of these patterns in every action line emitted by get_step_guidance().
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable

import pytest

# ── modules under test ──────────────────────────────────────────────────────
from skills.planner.architect.plan_design_execute import (
    STEPS as DESIGN_EXECUTE_STEPS,
)
from skills.planner.architect.plan_design_execute import (
    get_step_guidance as design_execute_guidance,
)
from skills.planner.architect.plan_design_qr_fix import (
    STEPS as DESIGN_QR_FIX_STEPS,
)
from skills.planner.architect.plan_design_qr_fix import (
    get_step_guidance as design_qr_fix_guidance,
)

# ── helpers ─────────────────────────────────────────────────────────────────

_UNSAFE_PATTERNS = ("--diff $'", "batch '[")


def _make_state_dir(qr_phase: str | None = None) -> str:
    """Create a temp dir with the minimal files sub-agent modules need."""
    tmp = tempfile.mkdtemp(prefix="inline_guard_test_")
    ctx = {"task": "test-task", "reference_docs": [], "decisions": []}
    with open(os.path.join(tmp, "context.json"), "w") as f:
        json.dump(ctx, f)
    if qr_phase:
        qr = {"phase": qr_phase, "iteration": 1, "items": []}
        with open(os.path.join(tmp, f"qr-{qr_phase}.json"), "w") as f:
            json.dump(qr, f)
    return tmp


def _flatten_actions(result: dict) -> list[str]:
    lines: list[str] = []
    for item in result.get("actions", []):
        if isinstance(item, str):
            lines.append(item)
        elif isinstance(item, list):
            for sub in item:
                if isinstance(sub, str):
                    lines.append(sub)
    return lines


# (label, guidance_fn, steps dict, qr_phase for state dir)
_MODULE_SPECS: list[tuple[str, Callable[..., dict], dict, str | None]] = [
    ("plan_design_execute", design_execute_guidance, DESIGN_EXECUTE_STEPS, None),
    ("plan_design_qr_fix", design_qr_fix_guidance, DESIGN_QR_FIX_STEPS, "plan-design"),
]


def _collect_actions(
    guidance_fn: Callable[..., dict], steps: dict, qr_phase: str | None
) -> list[str]:
    """Call guidance_fn for every step and flatten all action strings."""
    state_dir = _make_state_dir(qr_phase)
    try:
        lines: list[str] = []
        for step in steps:
            result = guidance_fn(step, state_dir=state_dir)
            lines.extend(_flatten_actions(result))
        return lines
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


# ── parametrised test ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "module_name,guidance_fn,steps,qr_phase",
    _MODULE_SPECS,
    ids=[s[0] for s in _MODULE_SPECS],
)
def test_no_inline_diff_or_batch(
    module_name: str,
    guidance_fn: Callable[..., dict],
    steps: dict,
    qr_phase: str | None,
) -> None:
    """No action line in any step of this module contains an unsafe inline form."""
    lines = _collect_actions(guidance_fn, steps, qr_phase)
    violations: list[str] = []
    for line in lines:
        for pattern in _UNSAFE_PATTERNS:
            if pattern in line:
                violations.append(f"  pattern={pattern!r}  line={line!r}")
    assert not violations, f"{module_name}: found unsafe inline shell forms:\n" + "\n".join(
        violations
    )


# ── self-check sentinel ──────────────────────────────────────────────────────


def test_sentinel_assertion_fires_on_synthetic_inline() -> None:
    """The assertion must fail when a synthetic unsafe line is injected.

    This proves the guard is actually exercised, not silently vacuous.
    """
    synthetic_lines = [
        '  uv run python -m skills.planner.cli.plan batch \'[{"method": "set-change"}]\'',
        "  uv run python -m skills.planner.cli.plan set-change --diff $'--- a/x.py\\n...'",
    ]
    for line in synthetic_lines:
        found = any(pat in line for pat in _UNSAFE_PATTERNS)
        assert found, (
            f"Sentinel check failed: synthetic unsafe line was NOT detected.\n"
            f"  line={line!r}\n"
            f"  patterns={_UNSAFE_PATTERNS}"
        )
