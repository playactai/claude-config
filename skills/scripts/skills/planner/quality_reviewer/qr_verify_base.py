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
from skills.planner.shared.qr.phases import get_phase_config, is_execution_phase
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
        item_offset = step - 2  # 0-indexed from step 2
        item_index = _item_index_for_step(step)
        phase = item_offset % 2  # 0=ANALYZE, 1=CONFIRM
        return ("ANALYZE" if phase == 0 else "CONFIRM", item_index)

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

    def _step_context(
        self, state_dir: str, module_path: str, item_ids: list[str], total_steps: int
    ) -> dict:
        """Step 1: Load conventions, phase rules, context.json, plan.json. List all items."""
        assert self.PHASE is not None
        state_dir_arg = f" --state-dir {state_dir}"
        item_flags = " ".join(f"--qr-item {id}" for id in item_ids)

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
            "next": f"uv run python -m {module_path} --step 2{state_dir_arg} {item_flags}",
        }

    def _step_analyze(
        self, state_dir: str, module_path: str, item_ids: list[str], item_idx: int, total_steps: int
    ) -> dict:
        """ANALYZE step: Explore codebase if needed, analyze item, form preliminary conclusion."""
        assert self.PHASE is not None
        state_dir_arg = f" --state-dir {state_dir}"
        item_flags = " ".join(f"--qr-item {id}" for id in item_ids)
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
            "next": f"uv run python -m {module_path} --step {current_step + 1}{state_dir_arg} {item_flags}",
        }

    def _step_confirm(
        self, state_dir: str, module_path: str, item_ids: list[str], item_idx: int, total_steps: int
    ) -> dict:
        """CONFIRM step: Verify confidence, record result via cli/qr.py."""
        assert self.PHASE is not None
        state_dir_arg = f" --state-dir {state_dir}"
        item_flags = " ".join(f"--qr-item {id}" for id in item_ids)
        current_step = 2 + (item_idx * 2) + 1  # CONFIRM is second of the pair

        item_id = item_ids[item_idx]
        qr_state = load_qr_state(state_dir, self.PHASE)
        item = get_qr_item(qr_state, item_id) if qr_state else None
        severity = item.get("severity", "SHOULD") if item else "SHOULD"

        # Determine next step
        next_step = current_step + 1
        if item_idx + 1 < len(item_ids):
            # More items to process
            next_action = (
                f"uv run python -m {module_path} --step {next_step}{state_dir_arg} {item_flags}"
            )
        else:
            # This was the last item, proceed to SUMMARY
            next_action = (
                f"uv run python -m {module_path} --step {next_step}{state_dir_arg} {item_flags}"
            )

        # Record the verdict via THIS script's --result flag (verify_main routes
        # it to cli.qr's locked update). One tool instead of two: the agent
        # records with the same command family it is already running, so the
        # natural guess succeeds (audit §3b NEW-C). --step pins which grouped
        # item the verdict applies to; pin_cwd keeps it cwd-independent.
        record_base = (
            f"uv run python -m {module_path} --step {current_step}{state_dir_arg} {item_flags}"
        )
        record_pass = pin_cwd(f"{record_base} --result PASS")
        record_fail = pin_cwd(f"{record_base} --result FAIL --finding '<one-line explanation>'")

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


def _item_index_for_step(step: int) -> int:
    """0-based item index for an ANALYZE/CONFIRM step.

    Steps 2..2N+1 pair ANALYZE (even offset) and CONFIRM (odd offset) per item,
    so both steps of item i map to index i. Single source of truth for the
    step<->item mapping, shared by VerifyBase._get_step_type (forward routing)
    and _resolve_target_item (recording a verdict) so the two cannot drift.
    CONTEXT (step 1) and SUMMARY (final step) are not item steps; callers gate
    those out before relying on the result.
    """
    return (step - 2) // 2


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
        idx = _item_index_for_step(step)
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


def verify_main(script_file: str, get_step_guidance, description: str, phase: str) -> None:
    """Entry point for QR verify scripts: mode_main plus a result-recording path.

    Without a result flag this behaves like lib.workflow.cli.mode_main: parse
    --step/--state-dir/--qr-item, route to the step handler, render via the
    shared render_step. With --result/--status PASS|FAIL (optionally --finding),
    it records the verdict directly and exits, so an agent appending the verdict
    to the verify command succeeds instead of erroring with 'unrecognized
    arguments' (audit §3b NEW-C). The CONFIRM step's own NEXT STEP pointer (read
    before recording) carries the agent onward.
    """
    import argparse

    from skills.lib.workflow.cli import _compute_module_path, render_step

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--step", type=int, default=None)
    parser.add_argument("--state-dir", type=str, default=None)
    parser.add_argument("--qr-item", action="append")
    parser.add_argument(
        "--result",
        "--status",
        dest="result",
        type=str,
        default=None,
        help="Record this item's verdict (PASS|FAIL) directly, then exit.",
    )
    parser.add_argument("--finding", type=str, default=None, help="Required with --result FAIL.")
    parsed = parser.parse_args()

    if parsed.result is not None:
        _record_verify_result(
            phase, parsed.step, parsed.state_dir, parsed.qr_item, parsed.result, parsed.finding
        )
        return

    if parsed.step is None:
        parser.error("--step is required")

    module_path = _compute_module_path(script_file)
    guidance = get_step_guidance(
        parsed.step,
        module_path,
        state_dir=parsed.state_dir or "",
        qr_item=parsed.qr_item,
    )
    if "error" in guidance:
        print(f"Error: {guidance['error']}", file=sys.stderr)
        sys.exit(1)
    print(render_step(parsed.step, guidance))
