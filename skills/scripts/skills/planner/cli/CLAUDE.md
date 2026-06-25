# cli/

Plan CLI: RPC commands, argparse commands, batch dispatch, and output formatting.

## Files

| File               | What                                                                      | When to read                                                     |
| ------------------ | ------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `plan_commands.py` | RPC functions (set_milestone, set_intent, set_decision, set_wave, etc.)   | Modifying plan mutation logic, debugging batch mode              |
| `plan.py`          | Argparse command classes mirroring the RPC functions; CLI entry point     | Modifying CLI UX, adding arguments, debugging single-invocation  |
| `plan_common.py`   | Shared primitives (validate_relpath, parse_csv, load/save plan)           | Adding shared validation, understanding plan I/O                 |
| `dispatch.py`      | RPC discovery, param extraction, single/batch dispatch, snapshot/restore  | Debugging batch execution, understanding method catalog          |
| `output.py`        | EntityResult, print_entity_result, VersionMismatchError, exit helpers     | Modifying output format, adding new result types                 |
| `qr_commands.py`   | QR-phase RPC functions                                                    | Modifying quality-review command logic                           |
| `qr_common.py`     | Shared QR primitives                                                      | Understanding QR state, modifying QR validation                  |
| `qr.py`            | Argparse command classes for QR phase                                     | Modifying QR CLI interface                                       |
| `verify.py`        | Final-verification recorder (writes verify.json)                          | Modifying final-verification gate, understanding verify.json     |
| `FOLLOWUPS.md`     | Residual findings and deferred work from batch/RPC review                  | Understanding known gaps, prioritizing follow-up work             |
| `__init__.py`      | Package init: re-exports qr, documents the read/write asymmetry            | Rarely (package layout)                                          |
