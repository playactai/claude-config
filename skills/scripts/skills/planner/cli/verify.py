"""CLI to record final-verification results into verify.json.

  uv run python -m skills.planner.cli.verify --state-dir <dir> \\
      --suite <pass|fail> --suite-summary '<line>' \\
      --lint  <pass|fail> --lint-summary  '<line>' \\
      --type  <pass|fail> --type-summary  '<line>'

Single writer (the executor's Final Verification step, not parallel agents), so
no flock is needed -- just an atomic write. This recorder is the trust-narrowing
layer for the LLM-asserted verdict: it requires ALL THREE checks in one call and a
typed pass|fail per check (an empty summary records a "(no output)" placeholder for
silent-success tools like tsc --noEmit); runs a light status/summary consistency
check; and bumps the failed-cycle counter that lets the gate escalate instead of
looping a red suite forever. It cannot make an LLM honest, but it makes the honest
record the only well-formed one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from typing import NoReturn
from xml.sax.saxutils import escape

from skills.lib.io import atomic_write_text
from skills.planner.shared.schema import VerifyFile, VerifyResult
from skills.planner.shared.verify_state import VERIFY_CHECKS, load_verify_state, verify_path

# A nonzero "N failed" / "N error(s)" in a summary contradicts a pass.
# Recognized formats only (pytest/ruff/pyright); anything unparseable trusts the
# explicit status but still stores the verbatim line for the audit trail.
_NONZERO_FAIL = re.compile(r"\b[1-9]\d*\s+(?:failed|error|errors)\b")
_EMPTY_SUMMARY_PLACEHOLDER = "(no output)"


def _error_exit(msg: str, code: int = 1) -> NoReturn:
    print(f"<verify_cli_error>\n  <message>{escape(msg)}</message>\n</verify_cli_error>")
    sys.exit(code)


def _summary_contradiction(status: str, summary: str) -> str | None:
    """Return a reason string when a 'pass' status contradicts a failing summary.

    The status flag is the routing source of truth; this is a weak tripwire
    that catches a mis-typed pass when the summary mentions failures/errors.
    A 'fail' status is never contradicted by a clean-looking summary -- a
    command can fail with zero errors (e.g. ESLint --max-warnings 0) or emit
    a digitless 'FAILED' -- so the fail-direction tripwire was removed.
    """
    s = summary.lower()
    if status == "pass" and _NONZERO_FAIL.search(s):
        return "summary reports failures/errors but status is 'pass'"
    return None


def _verify_fail_signature(results: list[VerifyResult]) -> str | None:
    """Stable fingerprint of the recorded FAIL set.

    Mirrors qr/utils._fail_signature (the sibling primitive) but hashes
    (check, summary) pairs rather than QR (id, version) because verify
    results carry no id/version fields. Returns None when no FAIL is
    recorded, so the idempotency bump never fires on an all-pass record.
    """
    pairs = sorted(
        [(r.check, r.summary) for r in results if r.status == "fail"]
    )
    if not pairs:
        return None
    return hashlib.sha256(json.dumps(pairs).encode("utf-8")).hexdigest()


def record(state_dir: str, statuses: dict[str, str], summaries: dict[str, str]) -> None:
    """Validate the three results and write verify.json atomically.

    iteration counts failed verify cycles: a record with any failure increments
    the prior count when the failure set differs from the last recorded one
    (idempotent re-record of the same FAIL set does not double-count); an
    all-pass record leaves it at >=1 (value then irrelevant -- the gate passes).
    An empty summary on a passing check is accepted with a deterministic
    placeholder for silent-success tools (e.g. tsc --noEmit).

    Mirrors qr/utils._fail_signature (the sibling primitive) for the
    fingerprint-based idempotency.
    """
    for check in VERIFY_CHECKS:
        summary = summaries[check].strip() or _EMPTY_SUMMARY_PLACEHOLDER
        contradiction = _summary_contradiction(statuses[check], summary)
        if contradiction:
            _error_exit(f"{check}: {contradiction} -- record the real result")

    # model_validate (not the kwarg ctor) so pyright accepts the str->Literal
    # narrowing; Pydantic still validates check/status against their Literals.
    results = [
        VerifyResult.model_validate(
            {"check": check, "status": statuses[check], "summary": summaries[check].strip() or _EMPTY_SUMMARY_PLACEHOLDER}
        )
        for check in VERIFY_CHECKS
    ]
    any_fail = any(r.status == "fail" for r in results)

    # iteration counts FAILED verify cycles (so the gate escalates at the ceiling)
    # via fingerprint-based idempotency: re-recording the identical FAIL set (no
    # intervening fix) recomputes the same signature and does NOT bump the counter.
    prior = load_verify_state(state_dir)
    prior_iter = prior.iteration if prior else 0
    sig = _verify_fail_signature(results)
    if any_fail:
        priorsig = prior.iteration_sig if prior else None
        new_iter = prior_iter + 1 if sig != priorsig else prior_iter
    else:
        new_iter = max(prior_iter, 1)

    vf = VerifyFile(
        iteration=new_iter,
        iteration_sig=(sig if any_fail else None),
        results=results,
    )
    atomic_write_text(verify_path(state_dir), vf.model_dump_json(indent=2))
    verdict = "FAIL" if any_fail else "PASS"
    print(f"verify.json recorded: {verdict} (iteration {new_iter})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record final-verification (suite/lint/type) results into verify.json",
    )
    parser.add_argument("--state-dir", required=True)
    for check in VERIFY_CHECKS:
        parser.add_argument(f"--{check}", choices=["pass", "fail"], required=True)
        parser.add_argument(f"--{check}-summary", required=True)
    args = parser.parse_args()

    statuses = {c: getattr(args, c) for c in VERIFY_CHECKS}
    summaries = {c: getattr(args, f"{c}_summary") for c in VERIFY_CHECKS}
    record(args.state_dir, statuses, summaries)


if __name__ == "__main__":
    main()
