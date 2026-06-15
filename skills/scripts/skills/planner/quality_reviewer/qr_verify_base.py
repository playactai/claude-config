"""Base class for QR verify scripts.

Definition locality + INTENT.md compliance:
- INTENT.md requires separate files per phase
- Base class provides shared logic (item loading, CLI invocation, output format)
- Subclasses override phase-specific verification logic

Dynamic step workflow based on item count:
- Formula: total_steps = 1 + (2 * num_items) + 1
- Step 1: CONTEXT (load shared state)
- Steps 2..2N+1: ANALYZE/CONFIRM pairs per item
- Final step: SUMMARY (aggregate results)

This works by:
1. Receive --qr-item a --qr-item b from orchestrator dispatch (argparse action="append")
2. Calculate total steps from item count
3. Route step number to (CONTEXT, ANALYZE, CONFIRM, SUMMARY)
4. ANALYZE: explore codebase, form preliminary conclusion
5. CONFIRM: verify confidence, record the verdict via this script's
   --result PASS|FAIL flag (verify_main delegates to cli/qr.py's locked update)
6. SUMMARY: aggregate pass/fail, output single word

Invariants:
- Verify agent mutates only assigned items
- PASS means check succeeded; no finding
- FAIL means check failed; finding explains what
- Recording delegates to cli/qr.py's locked update path; this script never
  writes qr-{phase}.json directly
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from typing import ClassVar

from skills.lib.workflow.prompts import pin_cwd
from skills.planner.shared.builders import shell_quote
from skills.planner.shared.qr.phases import (
    get_all_phases,
    get_phase_config,
    is_execution_phase,
)
from skills.planner.shared.qr.utils import (
    format_qr_item_for_verification,
    get_qr_item,
    load_qr_state,
)
from skills.planner.shared.resources import get_context_path, render_context_file


class VerifyBase(ABC):
    """Base class for QR verify scripts.

    Subclasses must:
    1. Set PHASE class attribute
    2. Override get_verification_guidance() with phase-specific verification instructions
    """

    PHASE: ClassVar[str] = ""  # Override in subclass

    def __init__(self):
        if not self.PHASE:
            raise ValueError("Subclass must set PHASE class attribute")
        self.config = get_phase_config(self.PHASE)

    @abstractmethod
    def get_verification_guidance(self, item: dict, state_dir: str) -> list[str]:
        """Return phase-specific verification instructions.

        Override in subclass with specific checks for this phase.

        Args:
            item: QR item dict with id, scope, check, status
            state_dir: Path to state directory

        Returns:
            List of instruction strings for the verification step
        """
        raise NotImplementedError

    def _temporal_contamination_guidance(self) -> list[str]:
        """Shared TEMPORAL CONTAMINATION block.

        Generated from the canonical TEMPORAL_DETECTION_QUESTIONS
        (shared/temporal_detection.py) so it always lists all five categories and
        their signals/actions, and cannot drift from the source of truth.
        """
        from skills.planner.shared.temporal_detection import TEMPORAL_DETECTION_QUESTIONS

        lines = [
            "TEMPORAL CONTAMINATION CHECK:",
            "  Scan comments in modified files for:",
        ]
        for q in TEMPORAL_DETECTION_QUESTIONS:
            signals = ", ".join(f"'{s}'" for s in q.signals)
            lines.append(f"  - {q.id}: {signals} -> {q.action}")
        lines.append("")
        return lines

    def _intent_marker_guidance(self, include_examples: bool = True) -> list[str]:
        """Shared INTENT MARKER VALIDATION block. See conventions/intent-markers.md."""
        lines = [
            "INTENT MARKER VALIDATION:",
            "  Valid format: ':MARKER: [what]; [why]'",
            "  - Must have semicolon",
            "  - Must have non-empty why after semicolon",
        ]
        if include_examples:
            lines += [
                "  Invalid: ':PERF: faster' (no semicolon)",
                "  Valid: ':PERF: faster; reduces API calls by 50%'",
            ]
        lines.append("")
        return lines

    def _get_step_type(self, step: int, num_items: int) -> tuple[str, int | None]:
        """Map step number to step type and item index.

        Step 1 is CONTEXT (load shared state).
        Steps 2 through 2N+1 alternate ANALYZE/CONFIRM per item.
        Final step is SUMMARY (aggregate results).

        Pure function: step number and item count determine step type and index.
        """
        if step == 1:
            return ("CONTEXT", None)
        final_step = 2 + (2 * num_items)
        if step == final_step:
            return ("SUMMARY", None)
        # Steps 2..final_step-1 are item steps
        item_index, parity = _item_index_for_step(step)
        return ("ANALYZE" if parity == 0 else "CONFIRM", item_index)

    def _get_total_steps(self, num_items: int) -> int:
        """Calculate total steps for N items: 1 + (2 * N) + 1."""
        return 2 + (2 * num_items)

    def get_step_guidance(self, step: int, module_path: str, **kwargs) -> dict:
        """Route to appropriate step handler based on step number and item count."""
        # action="append" returns list or None if not provided
        items = kwargs.get("qr_item") or []
        state_dir = kwargs.get("state_dir", "")

        if not items:
            return {
                "title": "Error",
                "actions": ["--qr-item required (repeatable: --qr-item a --qr-item b)"],
                "next": "",
            }
        if not state_dir:
            return {
                "title": "Error",
                "actions": ["--state-dir required"],
                "next": "",
            }

        num_items = len(items)
        total_steps = self._get_total_steps(num_items)
        step_type, item_idx = self._get_step_type(step, num_items)

        if step_type == "CONTEXT":
            return self._step_context(state_dir, module_path, items, total_steps)
        elif step_type == "ANALYZE":
            assert item_idx is not None
            return self._step_analyze(state_dir, module_path, items, item_idx, total_steps)
        elif step_type == "CONFIRM":
            assert item_idx is not None
            return self._step_confirm(state_dir, module_path, items, item_idx, total_steps)
        elif step_type == "SUMMARY":
            return self._step_summary(state_dir, module_path, items, total_steps)
        else:
            return {"error": f"Unknown step type for step {step}"}

    def _verify_cmd_args(self, state_dir: str, item_ids: list[str]) -> tuple[str, str, str]:
        """Build the (state-dir, phase, qr-item flags) CLI fragments shared by the
        verify re-invoke commands across steps 1/2/3.

        One source so the three steps cannot drift in quoting or flag spelling.
        """
        state_dir_arg = f" --state-dir {shell_quote(state_dir)}"
        phase_arg = f" --phase {self.PHASE}"
        item_flags = " ".join(f"--qr-item {shell_quote(id)}" for id in item_ids)
        return state_dir_arg, phase_arg, item_flags

    def _step_context(
        self, state_dir: str, module_path: str, item_ids: list[str], total_steps: int
    ) -> dict:
        """Step 1: Load conventions, phase rules, context.json, plan.json. List all items."""
        assert self.PHASE is not None
        state_dir_arg, phase_arg, item_flags = self._verify_cmd_args(state_dir, item_ids)

        # Execution-phase (impl-*) state dirs have no context.json (the executor
        # writes plan.json only), so degrade gracefully there; plan phases stay
        # strict. get_context_path always returns a Path, so render decides.
        context_file = get_context_path(state_dir)
        context_display = render_context_file(
            context_file, missing_ok=is_execution_phase(self.PHASE)
        )

        qr_state = load_qr_state(state_dir, self.PHASE)
        if not qr_state:
            return {
                "title": f"QR Verify Step 1/{total_steps}: Context ({self.PHASE})",
                "actions": [f"ERROR: Could not load qr-{self.PHASE}.json from {state_dir}"],
                "next": "",
            }

        # Load all items and display with severity
        items = []
        for item_id in item_ids:
            item = get_qr_item(qr_state, item_id)
            if not item:
                return {
                    "title": f"QR Verify Step 1/{total_steps}: Context ({self.PHASE})",
                    "actions": [f"ERROR: Item {item_id} not found in qr-{self.PHASE}.json"],
                    "next": "",
                }
            items.append(item)

        item_summary = []
        for item in items:
            severity = item.get("severity", "SHOULD")
            item_summary.append(f"  {item['id']} [{severity}]: {item.get('check', '')[:60]}")

        return {
            "title": f"QR Verify Step 1/{total_steps}: Context ({self.PHASE})",
            "actions": [
                f"PHASE: {self.PHASE}",
                f"ITEMS TO VERIFY: {len(items)}",
                "",
                *item_summary,
                "",
                "PLANNING CONTEXT (reference for semantic validation):",
                "",
                context_display,
                "",
                "UNDERSTAND the checks you need to perform.",
                "Note the scope: '*' means macro check, 'file:path:lines' means specific location.",
                "Severity indicates blocking behavior: MUST blocks all iterations, SHOULD blocks 1-3, COULD blocks 1-2.",
            ],
            "next": f"uv run python -m {module_path} --step 2{phase_arg}{state_dir_arg} {item_flags}",
        }

    def _step_analyze(
        self, state_dir: str, module_path: str, item_ids: list[str], item_idx: int, total_steps: int
    ) -> dict:
        """ANALYZE step: Explore codebase if needed, analyze item, form preliminary conclusion."""
        assert self.PHASE is not None
        state_dir_arg, phase_arg, item_flags = self._verify_cmd_args(state_dir, item_ids)
        current_step = 2 + (item_idx * 2)  # ANALYZE is first of the pair

        item_id = item_ids[item_idx]
        qr_state = load_qr_state(state_dir, self.PHASE)
        if not qr_state:
            return {
                "title": f"QR Verify Step {current_step}/{total_steps}: Analyze ({self.PHASE})",
                "actions": [f"ERROR: Could not load qr-{self.PHASE}.json"],
                "next": "",
            }

        item = get_qr_item(qr_state, item_id)
        if not item:
            return {
                "title": f"QR Verify Step {current_step}/{total_steps}: Analyze ({self.PHASE})",
                "actions": [f"ERROR: Item {item_id} not found"],
                "next": "",
            }

        item_display = format_qr_item_for_verification(item)
        severity = item.get("severity", "SHOULD")
        guidance = self.get_verification_guidance(item, state_dir)

        return {
            "title": f"QR Verify Step {current_step}/{total_steps}: Analyze {item_id} ({self.PHASE})",
            "actions": [
                f"ANALYZING: {item_id} (item {item_idx + 1} of {len(item_ids)})",
                f"SEVERITY: {severity}",
                "",
                item_display,
                "",
                "VERIFICATION GUIDANCE:",
                *guidance,
                "",
                "TASK:",
                "1. Read relevant files/sections based on scope",
                "2. Apply the verification check",
                "3. Form preliminary conclusion: PASS or FAIL?",
                "4. If FAIL, note specific evidence",
                "",
                "DO NOT update qr state yet. Proceed to CONFIRM step.",
            ],
            "next": f"uv run python -m {module_path} --step {current_step + 1}{phase_arg}{state_dir_arg} {item_flags}",
        }

    def _step_confirm(
        self, state_dir: str, module_path: str, item_ids: list[str], item_idx: int, total_steps: int
    ) -> dict:
        """CONFIRM step: Verify confidence, record result via cli/qr.py."""
        assert self.PHASE is not None
        state_dir_arg, phase_arg, item_flags = self._verify_cmd_args(state_dir, item_ids)
        current_step = 2 + (item_idx * 2) + 1  # CONFIRM is second of the pair

        item_id = item_ids[item_idx]
        qr_state = load_qr_state(state_dir, self.PHASE)
        item = get_qr_item(qr_state, item_id) if qr_state else None
        severity = item.get("severity", "SHOULD") if item else "SHOULD"

        # Next step is the next item's ANALYZE, or SUMMARY if this was the last
        # item -- both are current_step + 1 in the linear step sequence, so the
        # command is identical either way (one assignment, no dead branch).
        next_step = current_step + 1
        next_action = (
            f"uv run python -m {module_path} --step {next_step}{phase_arg}{state_dir_arg} {item_flags}"
        )

        # Record the verdict via THIS script's --result flag (verify_main routes
        # it to cli.qr's locked update). One tool instead of two: the agent
        # records with the same command family it is already running, so the
        # natural guess succeeds (audit §3b NEW-C). --step pins which grouped
        # item the verdict applies to; pin_cwd keeps it cwd-independent.
        record_base = (
            f"uv run python -m {module_path} --step {current_step}{phase_arg}{state_dir_arg} {item_flags}"
        )
        record_pass = pin_cwd(f"{record_base} --result PASS")
        # Double-quote the placeholder so apostrophes in findings survive the shell;
        # the actions line tells the agent how to escape any shell-special chars.
        record_fail = pin_cwd(f'{record_base} --result FAIL --finding "<one-line explanation>"')

        return {
            "title": f"QR Verify Step {current_step}/{total_steps}: Confirm {item_id} ({self.PHASE})",
            "actions": [
                f"CONFIRMING: {item_id} (item {item_idx + 1} of {len(item_ids)})",
                f"SEVERITY: {severity}",
                "",
                "CONFIDENCE CHECK:",
                "- Are you confident in your conclusion?",
                "- Did you verify against actual code/plan content?",
                "- Is your evidence specific and verifiable?",
                "",
                f"RECORD RESULT for {item_id} (run ONE, then run the NEXT STEP below):",
                "",
                "If PASS:",
                f"  {record_pass}",
                "",
                "If FAIL:",
                f"  {record_fail}",
                '  (Replace <one-line explanation> with your finding; backslash-escape any '
                '", $, \\, or backtick it contains so the shell preserves it.)',
                "",
                "Recording writes the verdict (lock-safe) and prints a confirmation;",
                "it does not advance the workflow -- run the NEXT STEP afterwards.",
            ],
            "next": next_action,
        }

    def _step_summary(
        self, state_dir: str, module_path: str, item_ids: list[str], total_steps: int
    ) -> dict:
        """SUMMARY step: Count results, output single word PASS or FAIL."""
        return {
            "title": f"QR Verify Step {total_steps}/{total_steps}: Summary ({self.PHASE})",
            "actions": [
                f"VERIFICATION COMPLETE: {len(item_ids)} items processed",
                "",
                "=" * 60,
                "FINAL OUTPUT FORMAT - READ THIS CAREFULLY",
                "=" * 60,
                "",
                "After processing all items, output EXACTLY ONE WORD:",
                "",
                "    PASS",
                "",
                "  or",
                "",
                "    FAIL",
                "",
                "RULES:",
                "- Your ENTIRE response after the CLI commands is ONE WORD",
                "- No markdown headers (## or **)",
                "- No 'VERDICT:' prefix",
                "- No explanation or reasoning",
                "- No prose of any kind",
                "- The finding/explanation goes in the --finding flag, NOT in your output",
                "",
                "WRONG outputs (DO NOT DO THIS):",
                "  '## VERDICT: FAIL'",
                "  '**FAIL**: The check failed because...'",
                "  'FAIL: M-002 lists buffer_test.go...'",
                "  'FAIL\\n\\nThe analysis shows...'",
                "",
                "CORRECT outputs (DO THIS):",
                "  'PASS'",
                "  'FAIL'",
                "",
                "If ANY item fails -> output: FAIL",
                "If ALL items pass -> output: PASS",
            ],
            "next": "",
        }


def _item_index_for_step(step: int) -> tuple[int, int]:
    """Return (item_index, parity) for an ANALYZE/CONFIRM step.

    Steps 2..2N+1 pair ANALYZE (parity 0) and CONFIRM (parity 1) per item,
    so both steps of item i map to index i. Single source of truth for the
    step<->item mapping, shared by VerifyBase._get_step_type (forward routing)
    and _resolve_target_item (recording a verdict) so the two cannot drift.
    CONTEXT (step 1) and SUMMARY (final step) are not item steps; callers gate
    those out before relying on the result.

    Returns (item_index, parity) where parity is 0 for ANALYZE or 1 for CONFIRM,
    so callers don't recompute `step - 2` separately.
    """
    offset = step - 2
    return offset // 2, offset % 2


def _resolve_target_item(step: int | None, items: list[str]) -> str:
    """Pick which item a --result flag refers to.

    A single --qr-item is unambiguous. For a grouped agent carrying several
    items, the CONFIRM step number identifies the target via
    _item_index_for_step (the same mapping VerifyBase._get_step_type uses for
    forward routing). Exits with a clear message when the target cannot be
    resolved rather than recording the wrong item.
    """
    if len(items) == 1:
        return items[0]
    if not items:
        sys.exit("Error: --qr-item is required to record a result.")
    if step is not None and step >= 2:
        idx, _ = _item_index_for_step(step)
        if 0 <= idx < len(items):
            return items[idx]
    sys.exit(
        "Error: multiple --qr-item values and no CONFIRM --step to disambiguate. "
        "Re-run at the item's CONFIRM step, or pass exactly one --qr-item with --result."
    )


def _record_verify_result(
    phase: str,
    step: int | None,
    state_dir: str | None,
    qr_items: list[str] | None,
    result: str,
    finding: str | None,
) -> None:
    """Record a verify verdict via the shared, lock-safe QR update path.

    Lets an agent record a result with the SAME script it is already running
    (`..._qr_verify ... --result PASS`) instead of switching to the separate
    `cli.qr update-item` tool -- the two-tool split made the natural guess
    hard-fail with 'unrecognized arguments' (audit §3b NEW-C). Delegates to
    cmd_update_item, which holds the phase write lock, validates the transition
    (FAIL needs a finding, PASS forbids one, PASS is terminal), writes
    atomically, and prints the structured result.
    """
    from skills.planner.cli.qr import cmd_update_item

    if not state_dir:
        sys.exit("Error: --state-dir is required to record a result.")

    item_id = _resolve_target_item(step, qr_items or [])
    update_args = [item_id, "--status", result.upper()]
    if finding:
        update_args += ["--finding", finding]
    cmd_update_item(state_dir, phase, update_args)


def decompose_main(script_file: str, get_step_guidance, description: str) -> None:
    """Entry point for the QR decompose runner: mode_main with --phase/--state-dir wired in.

    Mirrors verify_main's CLI wiring so qr_decompose.py doesn't re-declare the
    shared extra_args inline.
    """
    from skills.lib.workflow.cli import mode_main

    mode_main(
        script_file,
        get_step_guidance,
        description,
        extra_args=[
            (
                ["--phase"],
                {
                    "type": str,
                    "required": True,
                    "choices": get_all_phases(),
                    "help": "QR phase to decompose",
                },
            ),
            (["--state-dir"], {"type": str, "required": True, "help": "State directory path"}),
        ],
    )


def verify_main(script_file: str, get_step_guidance, description: str) -> None:
    """Entry point for the QR verify runner: mode_main plus a result-recording path.

    --phase selects the phase (the verifier and the qr-{phase}.json file) and is
    threaded through the emitted next/record commands, so a single runner serves
    all phases. Without a result flag it delegates to lib.workflow.cli.mode_main,
    which parses --step/--phase/--state-dir/--qr-item, routes to the step handler,
    and renders the step. With --result/--status PASS|FAIL (optionally --finding), it
    records the verdict directly and exits, so an agent appending the verdict to
    the verify command succeeds instead of erroring with 'unrecognized arguments'
    (audit §3b NEW-C). The CONFIRM step's own NEXT STEP pointer (read before
    recording) carries the agent onward.
    """
    from skills.lib.workflow.cli import mode_main

    def _pre_dispatch(parsed) -> bool:
        if getattr(parsed, "result", None) is not None:
            _record_verify_result(
                parsed.phase,
                parsed.step,
                parsed.state_dir,
                parsed.qr_item,
                parsed.result,
                parsed.finding,
            )
            return True
        return False

    mode_main(
        script_file,
        get_step_guidance,
        description,
        extra_args=[
            (
                ["--phase"],
                {
                    "type": str,
                    "required": True,
                    "choices": get_all_phases(),
                    "help": "QR phase (selects the verifier and the qr-{phase}.json file)",
                },
            ),
            (["--state-dir"], {"type": str, "default": None}),
            (["--qr-item"], {"action": "append"}),
            (
                ["--result", "--status"],
                {
                    "dest": "result",
                    "type": str,
                    "default": None,
                    "help": "Record this item's verdict (PASS|FAIL) directly, then exit.",
                },
            ),
            (
                ["--finding"],
                {"type": str, "default": None, "help": "Required with --result FAIL."},
            ),
        ],
        pre_dispatch=_pre_dispatch,
    )
