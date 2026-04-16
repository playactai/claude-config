# quality_reviewer/

Quality Review modules with QA state tracking integration; split into decompose/verify pairs per phase.

## Files

| File                         | What                                           | When to read                                           |
| ---------------------------- | ---------------------------------------------- | ------------------------------------------------------ |
| `README.md`                  | QR architecture, QA state tracking, LoopState  | Understanding QR workflow and response formats         |
| `qr_verify_base.py`          | VerifyBase ABC, dynamic step count, CLI wiring | Extending QR verify scripts, debugging dispatch        |
| `exec_reconcile.py`          | Plan vs implementation reconciliation          | Verifying existing code already satisfies milestones   |
| `plan_design_qr_decompose.py`| Generate QR items for plan-design phase        | Modifying design-phase quality checks                  |
| `plan_design_qr_verify.py`   | Verify QR items for plan-design phase          | Changing design-phase verification logic               |
| `plan_code_qr_decompose.py`  | Generate QR items for plan-code phase          | Modifying plan-code checks                             |
| `plan_code_qr_verify.py`     | Verify QR items for plan-code phase            | Changing plan-code verification logic                  |
| `plan_docs_qr_decompose.py`  | Generate QR items for plan-docs phase          | Modifying plan-docs checks                             |
| `plan_docs_qr_verify.py`     | Verify QR items for plan-docs phase            | Changing plan-docs verification logic                  |
| `impl_code_qr_decompose.py`  | Generate QR items for impl-code phase          | Modifying post-implementation code checks              |
| `impl_code_qr_verify.py`     | Verify QR items for impl-code phase            | Changing post-impl-code verification logic             |
| `impl_docs_qr_decompose.py`  | Generate QR items for impl-docs phase          | Modifying post-implementation docs checks              |
| `impl_docs_qr_verify.py`     | Verify QR items for impl-docs phase            | Changing post-impl-docs verification logic             |
| `__init__.py`                | Package marker                                 | Never (empty module)                                   |

## Subdirectories

| Directory  | What                                           | When to read                                           |
| ---------- | ---------------------------------------------- | ------------------------------------------------------ |
| `prompts/` | Shared prompt builders for decompose and verify | Modifying QR prompt wording, item formatting           |
