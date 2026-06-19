# technical_writer/

Technical writer sub-agent workflows: exec-docs phase only. Post-QR fix is the shared `quality_reviewer/exec_qr_fix.py` runner (`--phase impl-docs`). The TW authors ALL documentation directly in the real implemented source — inline comments, docstrings sourced from the Decision Log and Invisible Knowledge, plus CLAUDE.md and README updates.

## Files

| File                    | What                                                        | When to read                                              |
| ----------------------- | ----------------------------------------------------------- | --------------------------------------------------------- |
| `exec_docs.py`          | Router that dispatches to execute or fix for exec-docs      | Tracing how exec-docs work is routed                      |
| `exec_docs_execute.py`  | Post-implementation documentation authorship workflow       | Modifying how docs are authored in real source files      |
| `__init__.py`           | Package marker                                              | Never (empty module)                                      |
