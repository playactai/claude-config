"""Guard test for audit §3b NEW-A: every prose uv-run command is cwd-pinned.

For the plan-design sub-agent modules (the plan-phase work/fix scripts that emit
cli.plan commands after the rigid-diff redesign removed plan-code/plan-docs), and
for each of their steps, the test calls get_step_guidance() and flattens all string
lines in the returned actions list. It then asserts that every line containing
'uv run python -m skills.planner.cli' also contains 'cd ' at an earlier index,
proving the line is cwd-pinned.

A missing pin_cwd() call on any prose command will cause this test to fail.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable

import pytest

# ---------------------------------------------------------------------------
# Helper: build a minimal state dir for a given phase
# ---------------------------------------------------------------------------


def _make_state_dir(phase: str | None = None) -> str:
    """Create a temp dir with the files sub-agent modules may need."""
    tmp = tempfile.mkdtemp(prefix="newa_test_")
    ctx = {"task": "test-task", "reference_docs": [], "decisions": []}
    with open(os.path.join(tmp, "context.json"), "w") as f:
        json.dump(ctx, f)
    if phase:
        qr = {"phase": phase, "iteration": 1, "items": []}
        with open(os.path.join(tmp, f"qr-{phase}.json"), "w") as f:
            json.dump(qr, f)
    return tmp


def _flatten_actions(result: dict) -> list[str]:
    """Return every string in result['actions'], recursively flattened."""
    lines: list[str] = []
    for item in result.get("actions", []):
        if isinstance(item, str):
            lines.append(item)
        elif isinstance(item, list):
            for sub in item:
                if isinstance(sub, str):
                    lines.append(sub)
    return lines


def _assert_all_pinned(lines: list[str], context: str) -> None:
    """Assert every uv-run skills.planner.cli line is cwd-pinned."""
    MARKER = "uv run python -m skills.planner.cli"
    for line in lines:
        if MARKER in line:
            cd_idx = line.find("cd ")
            uv_idx = line.find(MARKER)
            assert cd_idx != -1 and cd_idx < uv_idx, (
                f"Unpinned command found in {context}:\n  {line!r}\n"
                f"Expected 'cd ' to appear before '{MARKER}'"
            )


# ---------------------------------------------------------------------------
# Module specifications
# ---------------------------------------------------------------------------

ModuleSpec = tuple[str, Callable[..., dict], list[int], str | None]

# (label, get_step_guidance_fn, step_list, qr_phase_for_state_dir)
MODULES: list[ModuleSpec] = []


def _register() -> None:
    from skills.planner.architect import plan_design_execute, plan_design_qr_fix

    MODULES.extend(
        [
            (
                "architect/plan_design_execute",
                plan_design_execute.get_step_guidance,
                list(plan_design_execute.STEPS.keys()),
                None,
            ),
            (
                "architect/plan_design_qr_fix",
                plan_design_qr_fix.get_step_guidance,
                list(plan_design_qr_fix.STEPS.keys()),
                "plan-design",
            ),
        ]
    )


_register()


def _parametrize_cases() -> list[tuple[str, Callable[..., dict], int, str | None]]:
    cases = []
    for label, fn, steps, qr_phase in MODULES:
        for step in steps:
            cases.append((f"{label}[step={step}]", fn, step, qr_phase))
    return cases


_CASES = _parametrize_cases()


@pytest.mark.parametrize("label,fn,step,qr_phase", _CASES, ids=[c[0] for c in _CASES])
def test_all_prose_commands_are_cwd_pinned(
    label: str,
    fn: Callable[..., dict],
    step: int,
    qr_phase: str | None,
) -> None:
    """Every 'uv run python -m skills.planner.cli' line must be preceded by 'cd '."""
    state_dir = _make_state_dir(qr_phase)
    try:
        result = fn(step, state_dir=state_dir)
    finally:
        import shutil

        shutil.rmtree(state_dir, ignore_errors=True)

    assert "error" not in result, f"get_step_guidance returned error for {label}: {result}"
    lines = _flatten_actions(result)
    _assert_all_pinned(lines, context=f"{label}")


def test_invariant_would_catch_missing_pin() -> None:
    """Confirm _assert_all_pinned raises when a command is unpinned."""
    unpinned_line = "  uv run python -m skills.planner.cli.plan --state-dir $X list"
    with pytest.raises(AssertionError, match="Unpinned command found"):
        _assert_all_pinned([unpinned_line], context="synthetic")


def test_invariant_passes_for_pinned_line() -> None:
    """Confirm _assert_all_pinned passes when a command is pinned."""
    pinned_line = "  cd /some/path && uv run python -m skills.planner.cli.plan list"
    _assert_all_pinned([pinned_line], context="synthetic")
