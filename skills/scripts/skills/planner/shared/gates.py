"""Unified gate output builder for planner and executor workflows.

Single implementation eliminates ~150 lines of duplicated gate logic.
Both planner.py and executor.py call this with their MODULE_PATH.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from skills.lib.workflow.prompts.step import SKILLS_DIR, format_step
from skills.planner.shared.builders import (
    ESCALATE_HANDLER,
    PEDANTIC_ENFORCEMENT,
    format_forbidden,
    format_gate_result,
    shell_quote,
)
from skills.planner.shared.qr.constants import QR_ITERATION_LIMIT
from skills.planner.shared.qr.types import AgentRole, QRState, QRStatus
from skills.planner.shared.qr.utils import (
    _blocking_items_from_state,
    by_blocking_severity,
    by_status,
    get_qr_iteration_from_state,
    has_qr_failures_from_state,
    load_qr_state,
    query_items,
)

if TYPE_CHECKING:
    from skills.planner.shared.schema import Plan

class _UnsetType:
    """Sentinel for qr_state default: distinguishes 'not provided' from None.

    The orchestrators thread their loaded dict-or-None; only a direct caller
    (e.g. tests) omits it, leaving _UNSET to trigger the self-load.
    """

    def __repr__(self) -> str:
        return "<_UNSET>"


_UNSET = _UnsetType()


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


def _unresolved_blocking_findings_from_state(qr_state: dict | None, iteration: int) -> list[str]:
    """Format the still-blocking FAIL items for the escalation prompt.

    Takes the pre-loaded qr_state dict build_gate_output already read, so the
    escalation path adds no second load of qr-{phase}.json.
    """
    if not qr_state:
        return []
    from skills.planner.shared.qr.utils import _fix_field_safe
    from skills.planner.shared.schema import canonicalize_severity

    lines: list[str] = []
    for item in query_items(qr_state, by_status("FAIL"), by_blocking_severity(iteration)):
        sev = canonicalize_severity(item.get("severity")) or "SHOULD"
        lines.append(f"  [{sev}] {item.get('id', '?')}: {_fix_field_safe(item.get('check', ''))}")
        if item.get("finding"):
            lines.append(f"        finding: {_fix_field_safe(item['finding'])}")
    return lines


def _has_recorded_failure_from_state(qr_state: dict | None) -> bool:
    """True when qr_state records at least one FAIL item (any severity).

    Distinguishes a *de-escalated* pass (FAIL items exist but none blocking at the
    current iteration -- the legitimate case that upgrades a severity-blind
    --qr-status fail to a pass) from an *absent* verdict (no FAIL recorded at all:
    a verifier returned FAIL without persisting its item, or items remain TODO).
    Only the former may upgrade an explicit fail to a pass. Takes the pre-loaded
    qr_state dict build_gate_output already read.
    """
    if not qr_state:
        return False
    return bool(query_items(qr_state, by_status("FAIL")))


def _has_blocking_todo_from_state(qr_state: dict | None) -> bool:
    """True when qr_state still has an unverified item that blocks now.

    Delegates to _blocking_items_from_state (the single read/filter pipeline
    shared with has_qr_failures_from_state) with status="TODO", so the iteration-default and
    severity logic lives in one place. Takes the pre-loaded qr_state dict
    build_gate_output already read.
    """
    return bool(_blocking_items_from_state(qr_state, "TODO"))


def _build_iteration_limit_escalation(
    module_path: str,
    qr_name: str,
    step: int,
    iteration: int,
    pass_step: int | None,
    state_dir: str,
    qr_state: dict | None = None,
) -> GateResult:
    """Build the user-escalation step shown when QR hits QR_ITERATION_LIMIT.

    De-escalation never drops MUST, so an unfixable MUST finding would loop the
    work -> verify -> route cycle forever. INTENT.md makes user authority
    absolute, so at the ceiling we stop and ask rather than dispatching another
    fix round. Rendered without format_step so it emits neither a
    "WORKFLOW COMPLETE" nor an imperative "NEXT STEP" footer -- the user's
    choice selects the next command.
    """
    findings = _unresolved_blocking_findings_from_state(qr_state, iteration)

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
        f"User authority is absolute (INTENT.md). Use {ESCALATE_HANDLER} to ask how to proceed:"
    )
    parts.append("")
    if pass_step is not None:
        accept_cmd = f"cd {shell_quote(str(SKILLS_DIR))} && uv run python -m {module_path} --step {pass_step}"
        if state_dir:
            accept_cmd += f" --state-dir {shell_quote(state_dir)}"
        parts.append(f"  Accept (proceed despite findings):\n    {accept_cmd}")
    else:
        # Terminal gate (planner, pass_step=None): "Accept" must FINALIZE the plan,
        # not just print prose. Re-invoke THIS gate step with --accept-findings so
        # build_gate_output forces a terminal pass and planner.main() renders plan.md
        # + saves it to docs/plans/. --qr-status pass is REQUIRED -- the planner
        # gate-step guard sys.exit(0)s before rendering when it is absent.
        accept_cmd = (
            f"cd {shell_quote(str(SKILLS_DIR))} && uv run python -m {module_path} "
            f"--step {step} --qr-status pass --accept-findings"
        )
        if state_dir:
            accept_cmd += f" --state-dir {shell_quote(state_dir)}"
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


def _build_completeness_block(
    module_path: str,
    qr_name: str,
    work_step: int,
    state_dir: str,
    errors: list[str],
    fix_target: AgentRole | None,
) -> GateResult:
    """Gate output when QR passed but the plan is structurally unexecutable.

    Routes back to the fixer (work_step) with the completeness errors, mirroring
    the executor's validate_completeness hard-exit but BEFORE approval -- so an
    approved plan is never saved to docs only to dead-end at execution.
    """
    target_name = fix_target.value if fix_target else "architect"
    parts = [
        format_gate_result(passed=False),
        "",
        "QR passed, but the plan is not executable as written. It fails the same",
        "structural contract the executor enforces (validate_completeness), which",
        "hard-exits on it:",
        "",
    ]
    parts.extend(f"  - {e}" for e in errors)
    parts.append("")
    parts.append(
        f"NEXT ACTION:\n"
        f"  Invoke the next step command.\n"
        f"  The next step will dispatch {target_name} to repair the plan structure "
        f"(e.g. author the missing execution waves)."
    )
    parts.append("")
    parts.append(
        format_forbidden(
            "Finalizing or saving the plan with these structural errors",
            "Treating wave coverage as optional",
            "Proceeding to execution -- the executor will reject this plan",
        )
    )
    body = "\n".join(parts)
    next_cmd = f"uv run python -m {module_path} --step {work_step} --state-dir {shell_quote(state_dir)}"
    return GateResult(
        output=format_step(body, next_cmd, title=f"{qr_name} Gate"),
        terminal_pass=False,
    )


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
    qr_state: dict | None | _UnsetType = _UNSET,
    plan: "Plan | None" = None,
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
    # Severity-aware on-disk state is the primary source of truth. The
    # agent-supplied qr.status (--qr-status) is a severity-blind PASS/FAIL
    # tally; past the de-escalation threshold it disagrees with the work step
    # and router (which read the same severity-aware blocking-FAIL state), so routing on it made the gate
    # dispatch a fixer while the work step ran first-time EXECUTE with no fix
    # context. Derive the verdict from the same predicate everyone else uses.
    # qr_state is loaded ONCE per gate by the calling frame (executor.format_output
    # / planner.get_step_guidance) and threaded in, then reused by every _from_state
    # predicate below (has_qr_failures_from_state, _has_recorded_failure_from_state,
    # _has_blocking_todo_from_state, _unresolved_blocking_findings_from_state); their
    # former state_dir-taking twins each re-read the file, yielding 4-5 redundant
    # open()+json.load() per gate. _UNSET means a direct caller (e.g. tests) skipped
    # the pre-load, so we self-load here to preserve standalone usability.
    resolved_state: dict | None = None
    has_blocking_fail = False  # raw blocking-FAIL verdict; reused by fail_step below
    if state_dir and phase:
        if qr_state is _UNSET:
            resolved_state = load_qr_state(state_dir, phase)
        elif qr_state is None:
            resolved_state = None
        else:
            resolved_state = qr_state  # type: ignore[assignment]
        if resolved_state is None:
            # Missing/corrupt qr-{phase}.json at gate time. Normal flow never reaches
            # here -- the verify step routes back to decompose on a missing file -- so
            # this is a manual deletion between verify and gate. Fail CLOSED: a gate
            # (especially the terminal one, pass_step=None) must never finalize a plan
            # whose QR cannot be confirmed. iteration = qr.iteration (1 in the missing-file
            # case) routes to work_step, which re-runs decompose and re-creates the file.
            passed = False
            iteration = qr.iteration
        else:
            has_blocking_fail = has_qr_failures_from_state(resolved_state)
            passed = not has_blocking_fail
            iteration = get_qr_iteration_from_state(resolved_state)
            # has_qr_failures_from_state reads False in two distinct situations: failures have
            # de-escalated below the blocking threshold (a real pass), OR nothing is
            # recorded yet -- a verifier returned FAIL without persisting its item, or
            # items are still TODO. An explicit --qr-status fail must not be silently
            # upgraded to a pass in the second case: with no recorded FAIL there is no
            # de-escalation to justify the upgrade, so the aggregator's failure stands.
            if (
                passed
                and qr.status == QRStatus.FAIL
                and not _has_recorded_failure_from_state(resolved_state)
            ):
                passed = False
            # A blocking item still at TODO is unverified blocking work, not a pass --
            # has_qr_failures_from_state counts only blocking FAIL, so it misses a blocking MUST whose
            # verifier returned FAIL/crashed before persisting. status-blind so it closes
            # both the --qr-status fail de-escalation veto above and the --qr-status pass
            # LLM tally. With no recorded blocking FAIL to fix, the fail routes to the
            # verify step (see the next_cmd selection below), which re-dispatches the
            # still-TODO item, so a transient verifier crash self-heals next pass; the loop is
            # intentionally un-ceilinged (iteration advances only on a recorded blocking FAIL).
            if passed and _has_blocking_todo_from_state(resolved_state):
                passed = False
    else:
        passed = qr.passed
        iteration = qr.iteration

    # --accept-findings only applies at the ceiling (the override just below is
    # gated on iteration >= QR_ITERATION_LIMIT). Passed earlier -- while the gate
    # still has fix iterations left -- it has no effect; warn to stdout instead of
    # silently dropping it, so a mistimed or copied override is visible rather than
    # appearing to have been honored.  The warning prints to stdout because the
    # orchestrator captures it inline in the step output.
    if accept_findings and iteration < QR_ITERATION_LIMIT:
        print(
            f"WARNING: --accept-findings ignored: gate is at iteration {iteration}, "
            f"below the ceiling ({QR_ITERATION_LIMIT}); the override only applies "
            f"at the ceiling."
        )

    # User accepted the findings AT THE CEILING: override to passed so the gate
    # neither re-escalates nor loops, and a terminal gate finalizes the plan. Gated
    # on the ceiling because the flag is only meaningful there (it is emitted solely
    # by the iteration-limit escalation) -- so a stray or copied --accept-findings
    # cannot silently pass a gate that has not yet exhausted its fix iterations.
    if accept_findings and iteration >= QR_ITERATION_LIMIT:
        passed = True

    # Deterministic structural gate: QR is LLM judgement, but an approved plan must
    # also satisfy the same completeness contract the executor enforces (and hard-
    # exits on). A QR-pass that is structurally unexecutable -- a code milestone in
    # no wave, a doc-only milestone left in a wave -- routes back to the fixer
    # instead of finalizing an unexecutable plan. Even --accept-findings cannot
    # waive this: the override is about QR finding severity, not structural validity.
    # validate_completeness self-gates by phase (single source of truth, no separate flag).
    if passed and phase:
        from skills.planner.shared.schema import plan_completeness_errors

        completeness_errors = plan_completeness_errors(state_dir, phase, plan=plan)
        if completeness_errors:
            return _build_completeness_block(
                module_path=module_path,
                qr_name=qr_name,
                work_step=work_step,
                state_dir=state_dir,
                errors=completeness_errors,
                fix_target=fix_target,
            )

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
            qr_state=resolved_state,
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
            next_cmd += f" --state-dir {shell_quote(state_dir)}"
    else:
        # A recorded blocking FAIL has a finding to fix -> work step (fix mode, which
        # the router and fix_mode derivation expect). A TODO-veto or status-tally
        # mismatch (no recorded blocking FAIL) has nothing concrete to fix -> re-verify,
        # which re-dispatches the still-TODO item so a transient verifier crash
        # self-heals. verify_step is always gate_step - 1 (work -> decompose -> verify -> gate).
        # When the file is missing (resolved_state is None), route to work_step so
        # decompose re-creates the file.
        fail_step = work_step if resolved_state is None or has_blocking_fail else step - 1
        next_cmd = f"uv run python -m {module_path} --step {fail_step} --state-dir {shell_quote(state_dir)}"

    return GateResult(
        output=format_step(body, next_cmd, title=title),
        terminal_pass=False,
    )
