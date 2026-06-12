# quality_reviewer/

Quality Review modules with QA state tracking integration. One phase-parameterized decompose runner and one verify runner serve all three QR phases (`--phase {plan-design|impl-code|impl-docs}`); phase-specific content lives in `prompts/content.py`.

## Files

| File                | What                                                                 | When to read                                           |
| ------------------- | ------------------------------------------------------------------- | ------------------------------------------------------ |
| `README.md`         | QR architecture, QA state tracking, LoopState                       | Understanding QR workflow and response formats         |
| `qr_verify_base.py` | VerifyBase ABC, dynamic step count, CLI wiring (`verify_main`)       | Extending QR verify, debugging dispatch                |
| `qr_decompose.py`   | `--phase` decompose runner (wires content to `prompts.dispatch_step`) | Changing decompose CLI/wiring                          |
| `qr_verify.py`      | `--phase` verify runner (selects a `VERIFIERS` subclass)             | Changing verify CLI/wiring                             |
| `__init__.py`       | Package marker                                                       | Never (empty module)                                   |

## Subdirectories

| Directory  | What                                                                            | When to read                                  |
| ---------- | ------------------------------------------------------------------------------- | --------------------------------------------- |
| `prompts/` | `decompose.py` (shared 13-step flow), `content.py` (per-phase prompts + verifier classes) | Modifying QR prompt wording, item formatting  |
