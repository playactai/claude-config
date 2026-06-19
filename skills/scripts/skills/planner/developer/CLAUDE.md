# developer/

Developer sub-agent workflows: exec-phase implementation with a router + execute pair. Post-QR fix is the shared `quality_reviewer/exec_qr_fix.py` runner (`--phase impl-code`). The developer generates implementation just-in-time per wave against the live file from Code Intent (`code_intents[]`).

## Files

| File                        | What                                                   | When to read                                                   |
| --------------------------- | ------------------------------------------------------ | -------------------------------------------------------------- |
| `exec_implement.py`         | Router that dispatches to execute or fix for impl-code | Tracing how implementation work is routed                      |
| `exec_implement_execute.py` | Wave-aware implementation workflow (exec phase)        | Modifying wave execution, parallel dispatch                    |
| `__init__.py`               | Package marker                                         | Never (empty module)                                           |
