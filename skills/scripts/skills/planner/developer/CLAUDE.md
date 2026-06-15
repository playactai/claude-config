# developer/

Developer sub-agent workflows: exec-phase implementation with a router + execute + QR-fix triple. The developer generates implementation just-in-time per wave against the live file from Code Intent (`code_intents[]`).

## Files

| File                        | What                                                   | When to read                                                   |
| --------------------------- | ------------------------------------------------------ | -------------------------------------------------------------- |
| `exec_implement.py`         | Router that dispatches to execute or fix for impl-code | Tracing how implementation work is routed                      |
| `exec_implement_execute.py` | Wave-aware implementation workflow (exec phase)        | Modifying wave execution, parallel dispatch                    |
| `exec_implement_qr_fix.py`  | Targeted repair for impl-code QR failures              | Changing how post-impl QR fixes are dispatched                 |
| `__init__.py`               | Package marker                                         | Never (empty module)                                           |

Routers call `route_work_phase()` from `planner/shared/routing.py` to detect fix mode from QR state; no `--qr-fail` flag required.
