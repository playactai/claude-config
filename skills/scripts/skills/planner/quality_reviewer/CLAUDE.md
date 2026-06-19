# quality_reviewer/

Quality Review modules with QA state tracking integration. One phase-parameterized decompose runner, one verify runner, and one fix runner serve all three QR phases (`--phase {plan-design|impl-code|impl-docs}`); phase-specific content lives in `prompts/content.py` (decompose + verify) and `prompts/fix.py` (fix).

## Files

| File                | What                                                                 | When to read                                           |
| ------------------- | ------------------------------------------------------------------- | ------------------------------------------------------ |
| `README.md`         | QR architecture, QA state tracking, LoopState                       | Understanding QR workflow and response formats         |
| `qr_verify_base.py` | VerifyBase ABC, dynamic step count, CLI wiring (`verify_main`/`decompose_main`/`fix_main`) | Extending QR verify, debugging dispatch                |
| `qr_decompose.py`   | `--phase` decompose runner (wires content to `prompts.dispatch_step`) | Changing decompose CLI/wiring                          |
| `qr_verify.py`      | `--phase` verify runner (selects a `VERIFIERS` subclass)             | Changing verify CLI/wiring                             |
| `exec_qr_fix.py`    | `--phase` fix runner (wires `FIX_CONTENT` to `prompts.fix.fix_dispatch_step`); the routers' `qr_fix` target | Changing fix CLI/wiring                                 |
| `__init__.py`       | Package marker                                                       | Never (empty module)                                   |

## Subdirectories

| Directory  | What                                                                            | When to read                                  |
| ---------- | ------------------------------------------------------------------------------- | --------------------------------------------- |
| `prompts/` | `decompose.py` (shared 13-step flow), `content.py` (per-phase decompose prompts + verifier classes), `fix.py` (shared 3-step fix flow + per-phase `FIX_CONTENT`) | Modifying QR prompt wording, item formatting  |
