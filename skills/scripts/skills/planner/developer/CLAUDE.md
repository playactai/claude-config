# developer/

Developer sub-agent workflows: plan-phase code filling and exec-phase implementation, each with a router + execute + QR-fix triple.

## Files

| File                        | What                                                   | When to read                                                   |
| --------------------------- | ------------------------------------------------------ | -------------------------------------------------------------- |
| `plan_code.py`              | Router that dispatches to execute or fix for plan-code | Tracing how plan-code work is routed                           |
| `plan_code_execute.py`      | First-time code filling workflow (plan phase)          | Modifying how code intent is filled into plan.json             |
| `plan_code_qr_fix.py`       | Targeted repair for plan-code QR failures              | Changing how plan-phase QR fixes are dispatched                |
| `exec_implement.py`         | Router that dispatches to execute or fix for impl-code | Tracing how implementation work is routed                      |
| `exec_implement_execute.py` | Wave-aware implementation workflow (exec phase)        | Modifying wave execution, parallel dispatch                    |
| `exec_implement_qr_fix.py`  | Targeted repair for impl-code QR failures              | Changing how post-impl QR fixes are dispatched                 |
| `__init__.py`               | Package marker                                         | Never (empty module)                                           |

Routers use `has_qr_failures()` from `planner/shared/qr/utils.py` to detect fix mode; no `--qr-fail` flag required.
