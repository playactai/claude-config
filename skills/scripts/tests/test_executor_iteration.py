"""Regression tests for executor fix-mode iteration and empty QR-verify branch.

Guards from the 2026-04-17 ultrareview:
- bug_001: executor fix-mode banner must read iteration from qr-{phase}.json,
  not the (now-removed) --qr-iteration CLI flag.
- bug_012: format_qr_verify's empty-items short-circuit must render a single
  NEXT STEP (no pass/fail branching) when there are no agents to dispatch.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from conftest import write_qr

from skills.planner.orchestrator import executor
from skills.planner.orchestrator.executor import format_output, format_qr_verify
from skills.planner.shared.qr.types import LoopState, QRState


class TestFixModeIterationBanner:
    """Fix-mode banners must reflect the iteration stored in qr-{phase}.json."""

    def test_step_2_banner_reads_iteration_3(self, tmp_path: Path):
        write_qr(
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
        write_qr(
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
        write_qr(
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
        write_qr(
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
        write_qr(
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


class TestReconciliationRigor:
    """--reconciliation-check restores the factored verification protocol inline
    (B1: the deleted exec_reconcile.py's rigor, not a bare 'mark satisfied' nudge).
    """

    def test_reconciliation_block_carries_factored_protocol(self, tmp_path: Path):
        out = format_output(
            step=1, state_dir=str(tmp_path), qr_status=None, reconciliation_check=True
        )
        assert "RECONCILIATION CHECK REQUESTED" in out
        assert "validate REQUIREMENTS, not code presence" in out  # the key distinction
        assert "MET | NOT_MET" in out                              # per-criterion record
        assert "OPEN questions" in out                             # anti-confirmation-bias
        assert "ALL its criteria are MET" in out                   # complete-only-when-all gate

    def test_reconciliation_absent_when_not_requested(self, tmp_path: Path):
        out = format_output(
            step=1, state_dir=str(tmp_path), qr_status=None, reconciliation_check=False
        )
        assert "RECONCILIATION CHECK REQUESTED" not in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
