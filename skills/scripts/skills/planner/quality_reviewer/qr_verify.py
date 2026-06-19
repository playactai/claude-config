#!/usr/bin/env python3
"""Phase-parameterized QR verification runner.

One runner for all three QR phases; --phase selects which VerifyBase subclass
(prompts/content.VERIFIERS) supplies per-item verification guidance. Replaces the
three near-identical *_qr_verify.py files. Shared step routing, CLI wiring, and
lock-safe result recording stay in qr_verify_base (verify_main).
"""

from skills.planner.quality_reviewer.prompts.content import get_verifier


def get_step_guidance(step: int, module_path: str, **kwargs) -> dict:
    """Delegate to the phase's verifier (which owns the empty-items guard)."""
    return get_verifier(kwargs["phase"]).get_step_guidance(step, module_path, **kwargs)


if __name__ == "__main__":
    from skills.planner.quality_reviewer.qr_verify_base import verify_main

    verify_main(
        __file__,
        get_step_guidance,
        "QR-Verify: Per-item verification for a QR phase (--phase selects)",
    )
