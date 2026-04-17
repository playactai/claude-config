"""Regression tests for executor fix-mode iteration and empty QR-verify branch.

Guards from the 2026-04-17 ultrareview:
- bug_001: executor fix-mode banner must read iteration from qr-{phase}.json,
  not the (now-removed) --qr-iteration CLI flag.
- bug_012: format_qr_verify's empty-items short-circuit must render a single
  NEXT STEP (no pass/fail branching) when there are no agents to dispatch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from skills.planner.orchestrator import executor
from skills.planner.orchestrator.executor import format_output, format_qr_verify
from skills.planner.shared.qr.types import LoopState, QRState


def _write_qr_state(state_dir: Path, phase: str, *, iteration: int, items: list[dict]) -> None:
    """Create qr-{phase}.json in state_dir with the given iteration and items."""
    (state_dir / f"qr-{phase}.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "phase": phase,
                "iteration": iteration,
                "items": items,
            }
        )
    )


class TestFixModeIterationBanner:
    """Fix-mode banners must reflect the iteration stored in qr-{phase}.json."""

    def test_step_2_banner_reads_iteration_3(self, tmp_path: Path):
        _write_qr_state(
            tmp_path,
            "impl-code",
            iteration=3,
            items=[
                {
                    "id": "impl-001",
                    "check": "x",
                    "status": "FAIL",
                    "severity": "MUST",
                    "scope": "*",
                    "finding": "broken",
                },
            ],
        )
        out = format_output(
            step=2, state_dir=str(tmp_path), qr_status=None, reconciliation_check=False
        )
        assert "iteration=3" in out
        assert "iteration=1" not in out
        assert "IMPLEMENTATION FIX" in out

    def test_step_6_banner_reads_iteration_4(self, tmp_path: Path):
        _write_qr_state(
            tmp_path,
            "impl-docs",
            iteration=4,
            items=[
                {
                    "id": "docs-001",
                    "check": "x",
                    "status": "FAIL",
                    "severity": "MUST",
                    "scope": "*",
                    "finding": "broken",
                },
            ],
        )
        out = format_output(
            step=6, state_dir=str(tmp_path), qr_status=None, reconciliation_check=False
        )
        assert "iteration=4" in out
        assert "DOCUMENTATION FIX" in out

    def test_no_fix_mode_when_state_file_absent(self, tmp_path: Path):
        """No qr-impl-code.json → first run, no fix banner."""
        out = format_output(
            step=2, state_dir=str(tmp_path), qr_status=None, reconciliation_check=False
        )
        assert "IMPLEMENTATION FIX" not in out
        assert "iteration=" not in out

    def test_no_fix_mode_when_all_pass(self, tmp_path: Path):
        _write_qr_state(
            tmp_path,
            "impl-code",
            iteration=1,
            items=[
                {
                    "id": "impl-001",
                    "check": "x",
                    "status": "PASS",
                    "severity": "MUST",
                    "scope": "*",
                    "finding": None,
                },
            ],
        )
        out = format_output(
            step=2, state_dir=str(tmp_path), qr_status=None, reconciliation_check=False
        )
        assert "IMPLEMENTATION FIX" not in out


class TestRemovedCliFlags:
    """--qr-iteration and --qr-fail were removed; argparse should reject them."""

    @pytest.mark.parametrize(
        "flag,value",
        [
            ("--qr-iteration", "5"),
            ("--qr-fail", None),
        ],
    )
    def test_rejected(
        self, flag: str, value: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        argv = ["executor", "--step", "2", "--state-dir", str(tmp_path), flag]
        if value is not None:
            argv.append(value)
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit):
            executor.main()


class TestEmptyQrVerifyRouting:
    """When all items are below blocking severity, format_qr_verify must
    collapse to a single NEXT STEP (no pass/fail branching prompt).
    """

    def test_empty_items_uses_single_next_step(self, tmp_path: Path):
        # iteration=5 → blocking={MUST}, so COULD items are filtered out.
        _write_qr_state(
            tmp_path,
            "impl-code",
            iteration=5,
            items=[
                {
                    "id": "impl-001",
                    "check": "trivial nit",
                    "status": "FAIL",
                    "severity": "COULD",
                    "scope": "*",
                    "finding": "cosmetic",
                },
            ],
        )
        qr = QRState(iteration=5, state=LoopState.INITIAL)
        out = format_qr_verify(step=4, phase="impl-code", state_dir=str(tmp_path), qr=qr)

        assert "NEXT STEP:" in out
        assert "NEXT STEP (MANDATORY -- execute exactly one)" not in out
        assert "Count PASS vs FAIL" not in out
        assert "ALL agents returned PASS" not in out
        assert "--qr-status pass" in out

    def test_non_empty_items_keeps_branching(self, tmp_path: Path):
        """Sanity check: when items remain, branching prompt is still used."""
        _write_qr_state(
            tmp_path,
            "impl-code",
            iteration=1,
            items=[
                {
                    "id": "impl-001",
                    "check": "real check",
                    "status": "TODO",
                    "severity": "MUST",
                    "scope": "*",
                },
            ],
        )
        qr = QRState(iteration=1, state=LoopState.INITIAL)
        out = format_qr_verify(step=4, phase="impl-code", state_dir=str(tmp_path), qr=qr)

        assert "NEXT STEP (MANDATORY -- execute exactly one)" in out
        assert "Count PASS vs FAIL" in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
