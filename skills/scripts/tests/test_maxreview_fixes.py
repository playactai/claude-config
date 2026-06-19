"""Regression tests for the two behavioral fixes from the max code review.

SW1: format_failed_items_for_fix is severity-aware (shows only the blocking FAILs
the gate routes on, not de-escalated non-blocking FAILs).
B1: the developer/TW execute scripts fail loud (ValueError) when state_dir is
missing at a step > 1, instead of silently emitting a command with no --state-dir.
"""

import pytest

from skills.planner.developer.exec_implement_execute import get_step_guidance as impl_execute
from skills.planner.shared.qr.utils import format_failed_items_for_fix
from skills.planner.technical_writer.exec_docs_execute import get_step_guidance as docs_execute


def test_fixer_prompt_lists_only_blocking_severity_fails():
    # iteration 4 -> only MUST blocks (get_blocking_severities); the SHOULD FAIL is
    # de-escalated and must NOT be presented to the fixer as a must-fix item.
    qr_state = {
        "iteration": 4,
        "items": [
            {"id": "q1", "status": "FAIL", "severity": "MUST", "check": "must check"},
            {"id": "q2", "status": "FAIL", "severity": "SHOULD", "check": "should check"},
        ],
    }
    block = format_failed_items_for_fix(qr_state)
    assert "q1" in block
    assert "q2" not in block


def test_fixer_prompt_lists_both_when_should_still_blocks():
    # iteration 1 -> MUST+SHOULD+COULD all block, so both FAILs are surfaced.
    qr_state = {
        "iteration": 1,
        "items": [
            {"id": "q1", "status": "FAIL", "severity": "MUST", "check": "must check"},
            {"id": "q2", "status": "FAIL", "severity": "SHOULD", "check": "should check"},
        ],
    }
    block = format_failed_items_for_fix(qr_state)
    assert "q1" in block
    assert "q2" in block


@pytest.mark.parametrize("guidance", [impl_execute, docs_execute])
def test_execute_step_one_is_permissive_without_state_dir(guidance):
    # Step 1 creates the state dir; requiring it would be circular.
    assert guidance(1)["title"]


@pytest.mark.parametrize("guidance", [impl_execute, docs_execute])
def test_execute_step_after_one_requires_state_dir(guidance):
    with pytest.raises(ValueError, match=r"state-dir required for step 2"):
        guidance(2)
