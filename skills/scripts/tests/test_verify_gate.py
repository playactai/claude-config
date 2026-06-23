"""Tests for the executor's Final Verification step + gate (steps 10/11/12).

Covers: the step-dispatch canary (a half-renumber that silently routed step 10
back to the retrospective would make the gate a no-op), the gate's deterministic
routing (green -> retrospective, fail -> reset QR + step 2, ceiling -> user
escalation, missing -> fail-closed re-verify), the cli/verify.py guardrails, and
step 2's verify-fix mode.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from conftest import write_verify

from skills.planner.orchestrator import executor as ex
from skills.planner.shared.qr.constants import QR_ITERATION_LIMIT
from skills.planner.shared.qr.types import LoopState, QRState
from skills.planner.shared.verify_state import load_verify_state, verify_all_pass

SCRIPTS = Path(__file__).parent.parent
GREEN = [("suite", "pass", "10 passed"), ("lint", "pass", "All checks passed!"), ("type", "pass", "0 errors")]
RED = [("suite", "fail", "9 passed, 1 failed"), ("lint", "pass", "All checks passed!"), ("type", "pass", "0 errors")]


def _fmt(step: int, state_dir) -> str:
    """Render an executor step via the public dispatch (qr/plan unused for 10/11/12)."""
    return ex.format_output(step, str(state_dir), None, False, None, None)


def _record(state_dir, suite="pass", lint="pass", typ="pass", suite_summary=None) -> subprocess.CompletedProcess:
    summaries = {
        "suite": suite_summary or ("1 failed" if suite == "fail" else "10 passed"),
        "lint": "2 errors" if lint == "fail" else "All checks passed!",
        "type": "1 error" if typ == "fail" else "0 errors",
    }
    return subprocess.run(
        [
            sys.executable, "-m", "skills.planner.cli.verify", "--state-dir", str(state_dir),
            "--suite", suite, "--suite-summary", summaries["suite"],
            "--lint", lint, "--lint-summary", summaries["lint"],
            "--type", typ, "--type-summary", summaries["type"],
        ],
        capture_output=True, text=True, cwd=SCRIPTS,
    )


# --- step-dispatch canary (guards the silent-misroute renumber bug) ----------

def test_step10_dispatches_to_final_verification(tmp_path):
    out = _fmt(10, tmp_path)
    assert "FINAL VERIFICATION" in out
    assert "cli.verify" in out
    assert "RETROSPECTIVE" not in out  # would mean step 10 silently == old retrospective


def test_step11_dispatches_to_verify_gate(tmp_path):
    assert "Final Verification Gate" in _fmt(11, tmp_path)


def test_step12_dispatches_to_retrospective(tmp_path):
    assert "RETROSPECTIVE" in _fmt(12, tmp_path)


def test_step13_is_invalid(tmp_path):
    assert "valid: 1-12" in _fmt(13, tmp_path)


def test_executor_main_rejects_out_of_range_step(tmp_path):
    r = subprocess.run(
        [sys.executable, "-m", "skills.planner.orchestrator.executor", "--step", "13",
         "--state-dir", str(tmp_path)],
        capture_output=True, text=True, cwd=SCRIPTS,
    )
    assert r.returncode != 0
    assert "1-12" in (r.stdout + r.stderr)


# --- gate routing ------------------------------------------------------------

def test_gate_green_routes_to_retrospective(tmp_path):
    write_verify(tmp_path, GREEN)
    out = _fmt(11, tmp_path)
    assert "--step 12" in out
    assert "verified" in out.lower()


def test_gate_fail_resets_qr_and_routes_to_step2(tmp_path):
    (tmp_path / "qr-impl-code.json").write_text('{"phase":"impl-code","items":[]}')
    (tmp_path / "qr-impl-docs.json").write_text('{"phase":"impl-docs","items":[]}')
    write_verify(tmp_path, RED, iteration=1)
    out = _fmt(11, tmp_path)
    assert "--step 2" in out
    # QR state cleared so code/doc QR re-decompose fresh against the fix.
    assert not (tmp_path / "qr-impl-code.json").exists()
    assert not (tmp_path / "qr-impl-docs.json").exists()


def test_gate_missing_verify_fails_closed_to_verify_step(tmp_path):
    out = _fmt(11, tmp_path)  # no verify.json
    assert "--step 10" in out
    assert "--step 12" not in out  # never finalizes without a green record


def test_gate_incomplete_verify_fails_closed(tmp_path):
    write_verify(tmp_path, [("suite", "pass", "10 passed")])  # only one check
    out = _fmt(11, tmp_path)
    assert "--step 10" in out


def test_gate_corrupt_verify_fails_closed(tmp_path):
    (tmp_path / "verify.json").write_text("{ not valid json")
    out = _fmt(11, tmp_path)
    assert "--step 10" in out
    assert "--step 12" not in out  # never finalizes on a garbage record


def test_gate_ceiling_escalates_to_user(tmp_path):
    write_verify(tmp_path, RED, iteration=QR_ITERATION_LIMIT)
    out = _fmt(11, tmp_path)
    assert "ITERATION LIMIT" in out
    assert "--step 12" in out  # accept option finalizes
    # escalation renders no automatic NEXT STEP footer -- the user chooses.
    assert "NEXT STEP" not in out


def test_step12_surfaces_outstanding_failures_when_accepted(tmp_path):
    # Reached via accept-at-ceiling: verify.json still red -> retrospective must
    # not silently report COMPLETED; it surfaces the outstanding failures.
    write_verify(tmp_path, RED, iteration=QR_ITERATION_LIMIT)
    out = _fmt(12, tmp_path)
    assert "OUTSTANDING VERIFICATION FAILURES" in out
    assert "9 passed, 1 failed" in out


# --- step 2 verify-fix mode --------------------------------------------------

def test_step2_renders_verify_fix_mode(tmp_path):
    write_verify(tmp_path, RED)
    out = ex.format_step_2(QRState(iteration=1, state=LoopState.INITIAL), str(tmp_path))
    assert "Verify Fix Mode" in out
    assert "Failing checks" in out
    assert "9 passed, 1 failed" in out


def test_step2_first_time_when_no_verify_failures(tmp_path):
    out = ex.format_step_2(QRState(iteration=1, state=LoopState.INITIAL), str(tmp_path))
    assert "Verify Fix Mode" not in out  # no verify.json -> normal first-time impl


def test_step2_code_fix_retry_beats_verify_fix(tmp_path):
    # A code-QR RETRY must win over a stale red verify.json: the executor is in a
    # code-QR fix loop, not a post-verify fix. Otherwise a code fix would silently
    # downgrade to verify-fix prose.
    write_verify(tmp_path, RED)
    out = ex.format_step_2(QRState(iteration=1, state=LoopState.RETRY), str(tmp_path))
    assert "Fix Mode" in out
    assert "Verify Fix Mode" not in out


def test_step2_first_time_when_verify_garbage(tmp_path):
    # A garbage verify.json is not a failure signal for step 2 (the gate owns the
    # fail-closed reroute); step 2 falls through to normal first-time impl.
    (tmp_path / "verify.json").write_text("{ not json")
    out = ex.format_step_2(QRState(iteration=1, state=LoopState.INITIAL), str(tmp_path))
    assert "Verify Fix Mode" not in out


# --- cli/verify.py guardrails ------------------------------------------------

def test_cli_records_and_bumps_iteration(tmp_path):
    assert _record(tmp_path, "pass", "pass", "pass").returncode == 0
    vf = load_verify_state(tmp_path)
    assert vf is not None and verify_all_pass(vf)
    # iteration counts FAILED cycles; the passing record above did not inflate it.
    assert _record(tmp_path, "fail", "pass", "pass").returncode == 0
    vf = load_verify_state(tmp_path)
    assert vf is not None and vf.iteration == 1  # first failed cycle
    assert _record(tmp_path, "fail", "pass", "pass").returncode == 0
    vf = load_verify_state(tmp_path)
    assert vf is not None and vf.iteration == 2  # second failed cycle bumps


def test_cli_accepts_realistic_green_summaries(tmp_path):
    # Guard the consistency regex against false-positiving on real tool output
    # (a future tightening could otherwise start rejecting valid green records).
    r = subprocess.run(
        [
            sys.executable, "-m", "skills.planner.cli.verify", "--state-dir", str(tmp_path),
            "--suite", "pass", "--suite-summary", "555 passed in 12.34s",
            "--lint", "pass", "--lint-summary", "All checks passed!",
            "--type", "pass", "--type-summary", "0 errors, 13 warnings, 0 informations",
        ],
        capture_output=True, text=True, cwd=SCRIPTS,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    vf = load_verify_state(tmp_path)
    assert vf is not None and verify_all_pass(vf)


def test_cli_requires_all_three_checks(tmp_path):
    r = subprocess.run(
        [sys.executable, "-m", "skills.planner.cli.verify", "--state-dir", str(tmp_path),
         "--suite", "pass", "--suite-summary", "10 passed"],
        capture_output=True, text=True, cwd=SCRIPTS,
    )
    assert r.returncode != 0  # argparse: --lint/--type required


def test_cli_rejects_bad_status(tmp_path):
    r = subprocess.run(
        [sys.executable, "-m", "skills.planner.cli.verify", "--state-dir", str(tmp_path),
         "--suite", "green", "--suite-summary", "x",
         "--lint", "pass", "--lint-summary", "x", "--type", "pass", "--type-summary", "x"],
        capture_output=True, text=True, cwd=SCRIPTS,
    )
    assert r.returncode != 0  # choices=[pass,fail]


def test_cli_consistency_guard_rejects_pass_with_failing_summary(tmp_path):
    r = _record(tmp_path, "pass", "pass", "pass", suite_summary="1 failed")
    assert r.returncode != 0
    assert "status is 'pass'" in r.stdout


def test_cli_consistency_guard_rejects_fail_with_clean_summary(tmp_path):
    r = subprocess.run(
        [sys.executable, "-m", "skills.planner.cli.verify", "--state-dir", str(tmp_path),
         "--suite", "fail", "--suite-summary", "10 passed",
         "--lint", "pass", "--lint-summary", "ok", "--type", "pass", "--type-summary", "0 errors"],
        capture_output=True, text=True, cwd=SCRIPTS,
    )
    assert r.returncode != 0
    assert "status is 'fail'" in r.stdout


def test_cli_rejects_empty_summary(tmp_path):
    r = _record(tmp_path, "pass", "pass", "pass", suite_summary="   ")
    assert r.returncode != 0
    assert "non-empty" in r.stdout
