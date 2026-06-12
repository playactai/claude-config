"""Unified gate output builder for planner and executor workflows.

Single implementation eliminates ~150 lines of duplicated gate logic.
Both planner.py and executor.py call this with their MODULE_PATH.
"""

import shlex
from dataclasses import dataclass

from skills.lib.workflow.prompts.step import SKILLS_DIR, format_step
from skills.planner.shared.builders import (
    PEDANTIC_ENFORCEMENT,
    format_forbidden,
    format_gate_result,
)
from skills.planner.shared.qr.constants import QR_ITERATION_LIMIT
from skills.planner.shared.qr.types import AgentRole, QRState
from skills.planner.shared.qr.utils import (
    by_blocking_severity,
    by_status,
    get_qr_iteration,
    has_qr_failures,
    load_qr_state,
    query_items,
)


@dataclass
class GateResult:
    """Return type for build_gate_output.

    Why dataclass over plain str: callers distinguish terminal passes
    (workflow done, run translate) from non-terminal passes (proceed to
    next phase). terminal_pass carries pass_step=None without requiring
    callers to re-derive it.
    """

    output: str
    terminal_pass: bool


def _unresolved_blocking_findings(state_dir: str, phase: str, iteration: int) -> list[str]:
    """Format the still-blocking FAIL items for the escalation prompt."""
    if not (state_dir and phase):
        return []
    qr_state = load_qr_state(state_dir, phase)
    if not qr_state:
        return []
    lines: list[str] = []
    for item in query_items(qr_state, by_status("FAIL"), by_blocking_severity(iteration)):
        sev = str(item.get("severity") or "SHOULD").strip().upper()
        lines.append(f"  [{sev}] {item.get('id', '?')}: {item.get('check', '')}")
        if item.get("finding"):
            lines.append(f"        finding: {item['finding']}")
    return lines


def _build_iteration_limit_escalation(
    module_path: str,
    qr_name: str,
    step: int,
    iteration: int,
    pass_step: int | None,
    state_dir: str,
    phase: str,
) -> GateResult:
    """Build the user-escalation step shown when QR hits QR_ITERATION_LIMIT.

    De-escalation never drops MUST, so an unfixable MUST finding would loop the
    work -> verify -> route cycle forever. INTENT.md makes user authority
    absolute, so at the ceiling we stop and ask rather than dispatching another
    fix round. Rendered without format_step so it emits neither a
    "WORKFLOW COMPLETE" nor an imperative "NEXT STEP" footer -- the user's
    choice selects the next command.
    """
    findings = _unresolved_blocking_findings(state_dir, phase, iteration)

    parts = [
        format_gate_result(passed=False),
        "",
        f"QR REACHED THE ITERATION LIMIT ({QR_ITERATION_LIMIT}).",
        "",
        f"Blocking findings still unresolved after {iteration} iterations:",
        "",
    ]
    parts.extend(findings or ["  (see qr state; no per-item findings recorded)"])
    parts.append("")
    parts.append(
        "ESCALATE TO USER -- the workflow will NOT loop again on its own.\n"
        "User authority is absolute (INTENT.md). Use AskUserQuestion to ask how to proceed:"
    )
    parts.append("")
    if pass_step is not None:
        accept_cmd = f"cd {shlex.quote(str(SKILLS_DIR))} && uv run python -m {module_path} --step {pass_step}"
        if state_dir:
            accept_cmd += f" --state-dir {shlex.quote(state_dir)}"
        parts.append(f"  Accept (proceed despite findings):\n    {accept_cmd}")
    else:
        # Terminal gate (planner, pass_step=None): "Accept" must FINALIZE the plan,
        # not just print prose. Re-invoke THIS gate step with --accept-findings so
        # build_gate_output forces a terminal pass and planner.main() renders plan.md
        # + saves it to docs/plans/. --qr-status pass is REQUIRED -- the planner
        # gate-step guard sys.exit(0)s before rendering when it is absent.
        accept_cmd = (
            f"cd {shlex.quote(str(SKILLS_DIR))} && uv run python -m {module_path} "
            f"--step {step} --qr-status pass --accept-findings"
        )
        if state_dir:
            accept_cmd += f" --state-dir {shlex.quote(state_dir)}"
        parts.append(f"  Accept (approve the plan as-is and finalize):\n    {accept_cmd}")
    parts.append("  Abort: stop here. Do NOT invoke a next step; report the findings to the user.")
    parts.append("")
    parts.append(
        format_forbidden(
            "Looping back to the fixer automatically",
            "Proceeding without an explicit user decision",
            "Hiding or downgrading the unresolved findings",
        )
    )

    title = f"{qr_name} Gate -- Iteration Limit Reached"
    body = f"{title}\n{'=' * len(title)}\n\n" + "\n".join(parts)
    return GateResult(output=body, terminal_pass=False)


def build_gate_output(
    module_path: str,
    qr_name: str,
    qr: QRState,
    step: int,
    work_step: int,
    pass_step: int | None,
    pass_message: str,
    fix_target: AgentRole | None,
    state_dir: str,
    phase: str,
    accept_findings: bool = False,
) -> GateResult:
    """Build complete gate step output for QR gates.

    Gates route to either:
    - pass_step: QR passed, proceed to next workflow phase
    - work_step: QR failed, loop back to fix issues
    - user escalation: QR still failing at the iteration ceiling

    accept_findings is the user's explicit ceiling override (INTENT.md: user
    authority is absolute): treat QR as passed despite unresolved findings, so a
    terminal gate (pass_step=None) finalizes the plan instead of re-escalating.
    """
    # Severity-aware on-disk state is the single source of truth. The
    # agent-supplied qr.status (--qr-status) is a severity-blind PASS/FAIL
    # tally; past the de-escalation threshold it disagrees with the work step
    # and router (which read has_qr_failures), so routing on it made the gate
    # dispatch a fixer while the work step ran first-time EXECUTE with no fix
    # context. Derive the verdict from the same predicate everyone else uses.
    # (The else is a defensive fallback for missing state -- every real gate is
    # invoked with a non-empty state_dir and phase.)
    if state_dir and phase:
        passed = not has_qr_failures(state_dir, phase)
        iteration = get_qr_iteration(state_dir, phase)
    else:
        passed = qr.passed
        iteration = qr.iteration

    # User accepted the findings AT THE CEILING: override to passed so the gate
    # neither re-escalates nor loops, and a terminal gate finalizes the plan. Gated
    # on the ceiling because the flag is only meaningful there (it is emitted solely
    # by the iteration-limit escalation) -- so a stray or copied --accept-findings
    # cannot silently pass a gate that has not yet exhausted its fix iterations.
    if accept_findings and iteration >= QR_ITERATION_LIMIT:
        passed = True

    # Iteration ceiling: de-escalation never drops MUST, so an unfixable MUST
    # blocks every iteration and the work -> verify -> route loop would run
    # forever. At the limit, hand control to the user instead of looping again.
    if not passed and iteration >= QR_ITERATION_LIMIT:
        return _build_iteration_limit_escalation(
            module_path=module_path,
            qr_name=qr_name,
            step=step,
            iteration=iteration,
            pass_step=pass_step,
            state_dir=state_dir,
            phase=phase,
        )

    parts = []
    parts.append(format_gate_result(passed=passed))
    parts.append("")

    if passed:
        parts.append(pass_message)
        parts.append("")
        parts.append(
            format_forbidden(
                "Asking the user whether to proceed - the workflow is deterministic",
                "Offering alternatives to the next step - all steps are mandatory",
                "Interpreting 'proceed' as optional - EXECUTE immediately",
            )
        )
    else:
        parts.append(PEDANTIC_ENFORCEMENT)
        parts.append("")
        target_name = fix_target.value if fix_target else "developer"
        parts.append(
            f"NEXT ACTION:\n"
            f"  Invoke the next step command.\n"
            f"  The next step will dispatch {target_name} with fix guidance."
        )
        parts.append("")
        parts.append(
            format_forbidden(
                "Fixing issues directly from this gate step",
                "Spawning agents directly from this gate step",
                "Using Edit/Write tools yourself",
                "Proceeding without invoking the next step",
                "Interpreting 'minor issues' as skippable",
                "Claiming 'diminishing returns' or 'comprehensive enough'",
                "Proceeding to next phase without QR PASS",
            )
        )

    body = "\n".join(parts)
    title = f"{qr_name} Gate"
    terminal_pass = passed and pass_step is None

    if terminal_pass:
        return GateResult(output=format_step(body, title=title), terminal_pass=True)

    if passed:
        next_cmd = f"uv run python -m {module_path} --step {pass_step}"
        if state_dir:
            next_cmd += f" --state-dir {shlex.quote(state_dir)}"
    else:
        next_cmd = f"uv run python -m {module_path} --step {work_step} --state-dir {shlex.quote(state_dir)}"

    return GateResult(
        output=format_step(body, next_cmd, title=title),
        terminal_pass=False,
    )
