#!/usr/bin/env python3
"""Phase-parameterized QR decomposition runner.

One runner for all three QR phases (plan-design, impl-code, impl-docs); --phase
selects which phase's prompt content (prompts/content.DECOMPOSE_CONTENT) drives
the shared 13-step dispatch_step. Replaces the three near-identical
*_qr_decompose.py files -- the control flow already lived in
prompts/decompose.dispatch_step, so this file only wires --phase to its content.
"""

from skills.planner.quality_reviewer.prompts.content import get_decompose_content
from skills.planner.quality_reviewer.prompts.decompose import dispatch_step


def get_step_guidance(step: int, module_path: str, **kwargs) -> dict:
    phase = kwargs["phase"]
    state_dir = kwargs.get("state_dir", "")
    content = get_decompose_content(phase)
    return dispatch_step(
        step,
        phase,
        module_path,
        content["phase_prompts"],
        content["grouping_config"],
        state_dir,
    )


if __name__ == "__main__":
    from skills.lib.workflow.cli import mode_main
    from skills.planner.shared.qr.phases import get_all_phases

    mode_main(
        __file__,
        get_step_guidance,
        "QR-Decompose: Generate verification items for a QR phase (--phase selects)",
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
