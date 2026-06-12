# technical_writer/

Technical writer sub-agent workflows: exec-docs phase only. The TW authors ALL documentation directly in the real implemented source — inline comments, docstrings sourced from the Decision Log and Invisible Knowledge, plus CLAUDE.md and README updates.

## Files

| File                    | What                                                        | When to read                                              |
| ----------------------- | ----------------------------------------------------------- | --------------------------------------------------------- |
| `exec_docs.py`          | Router that dispatches to execute or fix for exec-docs      | Tracing how exec-docs work is routed                      |
| `exec_docs_execute.py`  | Post-implementation documentation authorship workflow       | Modifying how docs are authored in real source files      |
| `exec_docs_qr_fix.py`   | Targeted repair for impl-docs QR failures                   | Changing how post-docs QR fixes are dispatched            |
| `__init__.py`           | Package marker                                              | Never (empty module)                                      |

Routers use `has_qr_failures()` from `planner/shared/qr/utils.py` to detect fix mode; no `--qr-fail` flag required.
