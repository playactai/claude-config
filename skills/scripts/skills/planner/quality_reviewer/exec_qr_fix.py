#!/usr/bin/env python3
"""Phase-parameterized QR fix runner.

One runner for all three QR phases (plan-design, impl-code, impl-docs); --phase
selects which phase's fix content (prompts/fix.FIX_CONTENT) drives the shared
3-step fix_dispatch_step. Replaces the three near-identical *_qr_fix.py files --
the control flow and step-1 load already factor into prompts/fix, so this file
only wires --phase to its content. The work-phase routers reach it via
shared.routing.WORK_PHASES["*"]["qr_fix"].
"""

from skills.planner.quality_reviewer.prompts.fix import fix_dispatch_step, get_fix_content


def get_step_guidance(step: int, module_path: str, **kwargs) -> dict:
    phase = kwargs["phase"]
    state_dir = kwargs.get("state_dir", "")
    return fix_dispatch_step(step, phase, module_path, get_fix_content(phase), state_dir)


if __name__ == "__main__":
    from skills.planner.quality_reviewer.qr_verify_base import fix_main

    fix_main(
        __file__,
        get_step_guidance,
        "QR-Fix: Targeted repair for a QR phase's failures (--phase selects)",
    )
