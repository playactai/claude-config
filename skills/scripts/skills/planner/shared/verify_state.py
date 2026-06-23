"""Final-verification state (verify.json) helpers.

verify.json records the executor's final suite/lint/type results: written by
cli/verify.py from the LLM-run command output, read by the executor's Final
Verification gate. Deliberately kept out of validate_state and the QR machinery
-- it is a small binary record the gate reads fail-closed (a missing or
unparseable file counts as "not green", never as a pass).
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from skills.planner.shared.qr.utils import _fix_field_safe
from skills.planner.shared.schema import VerifyFile, VerifyResult

VERIFY_FILENAME = "verify.json"
VERIFY_CHECKS: tuple[str, ...] = ("suite", "lint", "type")
_CHECK_LABEL = {"suite": "Test suite", "lint": "Lint", "type": "Type check"}


def verify_path(state_dir: str) -> Path:
    return Path(state_dir) / VERIFY_FILENAME


def load_verify_state(state_dir: str) -> VerifyFile | None:
    """Read + validate verify.json. Returns None (fail-closed) on missing/garbage.

    The gate treats None as "not green" and reroutes to re-run verification, so a
    corrupt/partial record self-heals on the next verify step (which overwrites it).
    """
    path = verify_path(state_dir)
    if not path.exists():
        return None
    try:
        return VerifyFile.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def verify_is_complete(vf: VerifyFile) -> bool:
    """True only when all three checks (suite/lint/type) are recorded exactly once."""
    return {r.check for r in vf.results} == set(VERIFY_CHECKS)


def verify_failures(vf: VerifyFile) -> list[VerifyResult]:
    return [r for r in vf.results if r.status == "fail"]


def verify_all_pass(vf: VerifyFile) -> bool:
    """True when the record is complete AND every check passed."""
    return verify_is_complete(vf) and not verify_failures(vf)


def verify_has_failures(state_dir: str) -> bool:
    """True when verify.json exists and records at least one failing check.

    Used by the executor's step-2 verify-fix branch. A missing/garbage record
    (None) is NOT a failure here -- the gate, not step 2, owns the fail-closed
    reroute when the record is absent or incomplete.
    """
    vf = load_verify_state(state_dir)
    return bool(vf and verify_failures(vf))


def format_verify_failures_for_fix(vf: VerifyFile) -> str:
    """Render failing checks + summaries for a developer fix / gate prompt.

    Summaries are pasted command output flowing into a plaintext prompt, so each
    is neutralized (the same line-forging defense QR findings use at every sink).
    """
    lines = [
        f"  [{_CHECK_LABEL.get(r.check, r.check)}] {_fix_field_safe(r.summary)}"
        for r in verify_failures(vf)
    ]
    return "\n".join(lines) if lines else "  (no failing checks recorded)"


def reset_qr_for_reverify(state_dir: str) -> None:
    """Delete qr-impl-code.json + qr-impl-docs.json so the next code/doc QR
    decompose regenerates fresh items against the fixed code.

    This is what makes a post-verify fix get a genuine FRESH QR review (catching
    bugs the fix introduces) rather than a re-verify of stale all-PASS items.
    Best-effort: a missing file is fine. The outer verify.json iteration counter
    bounds the total fix cycles, so resetting these inner counters cannot wedge
    the loop.
    """
    for phase in ("impl-code", "impl-docs"):
        with contextlib.suppress(OSError):
            (Path(state_dir) / f"qr-{phase}.json").unlink(missing_ok=True)
