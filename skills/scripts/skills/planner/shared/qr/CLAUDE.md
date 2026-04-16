# qr/

QR-specific domain types, phase configurations, CLI helpers, and state-file utilities used by orchestrators and QR sub-agent scripts.

## Files

| File          | What                                                                          | When to read                                                       |
| ------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `phases.py`   | `QR_PHASES` registry + `get_phase_config()` — single source of truth for each phase's decompose/verify script paths and step numbers | Adding a phase, changing script paths                              |
| `constants.py`| QR routing table, blocking-severity tiers per iteration                       | Changing severity thresholds or route targets                      |
| `types.py`    | `QRState`, `QRStatus`, `LoopState`, `AgentRole`                               | Adding QR state fields, new agent roles                            |
| `utils.py`    | `load_qr_state`, `query_items`, `by_status`, `by_blocking_severity`, `increment_qr_iteration`, `has_qr_failures`, `qr_file_exists`, formatting helpers | Reading/writing qr-{phase}.json, filtering items by status/severity |
| `cli.py`      | `add_qr_args()` — shared argparse wiring for QR scripts                       | Adding shared CLI args to QR scripts                               |
| `__init__.py` | Public API re-exports                                                         | Importing QR types/utils                                           |
