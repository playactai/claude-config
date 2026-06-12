#!/usr/bin/env python3
"""Phase-parameterized QR verification runner.

One runner for all three QR phases; --phase selects which VerifyBase subclass
(prompts/content.VERIFIERS) supplies per-item verification guidance. Replaces the
three near-identical *_qr_verify.py files. Shared step routing, CLI wiring, and
lock-safe result recording stay in qr_verify_base (verify_main).
"""

from skills.planner.quality_reviewer.prompts.content import get_verifier

MODULE_PATH = "skills.planner.quality_reviewer.qr_verify"


def get_step_guidance(step: int, module_path: str | None = None, **kwargs) -> dict:
    """Gateway normalizes input and delegates to the phase's verifier."""
    module_path = module_path or MODULE_PATH
    phase = kwargs["phase"]
    qr_item = kwargs.get("qr_item")

    if qr_item:
        # Normalize to list (backwards compat if single string passed)
        items = qr_item if isinstance(qr_item, list) else [qr_item]
        kwargs["qr_item"] = items
        verifier = get_verifier(phase)
        return verifier.get_step_guidance(step, module_path, **kwargs)

    return {
        "title": "Error: No Items",
        "actions": ["--qr-item required. Use: --qr-item a --qr-item b"],
        "next": "",
    }


if __name__ == "__main__":
    from skills.planner.quality_reviewer.qr_verify_base import verify_main

    verify_main(
        __file__,
        get_step_guidance,
        "QR-Verify: Per-item verification for a QR phase (--phase selects)",
    )
