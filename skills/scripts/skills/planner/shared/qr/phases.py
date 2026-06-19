"""Single source of truth for QR phase configurations.

Definition locality: understanding a phase's configuration requires
reading only THIS file. Scripts import from here instead of duplicating
phase-specific knowledge across 4+ files per phase.

This works by:
1. QR_PHASES dict defines all phase configurations
2. get_phase_config() provides single entry point
3. Scripts import from here, not from each other
4. Changes to phase config require editing only THIS file

Invariants:
- Each phase has exactly one entry in QR_PHASES
- Script paths are valid Python module paths
- Artifact paths are relative to state_dir
"""

from __future__ import annotations

# Phase configuration registry - ALL phase definitions in ONE place
#
# Keys: phase name as used in qr-{phase}.json filename
# Values: dict with:
#   workflow: "planner" or "executor"
#   artifact: primary artifact being reviewed (relative to state_dir)
#   decompose_script: Python module for decomposition (phase-parameterized runner,
#                     dispatched with --phase {name}; same module for every phase)
#   verify_script: Python module for single-item verification (same --phase runner)

QR_PHASES: dict[str, dict] = {
    "plan-design": {
        "workflow": "planner",
        "artifact": "plan.json",
        "decompose_script": "skills.planner.quality_reviewer.qr_decompose",
        "verify_script": "skills.planner.quality_reviewer.qr_verify",
    },
    "impl-code": {
        "workflow": "executor",
        "artifact": "plan.json",
        "decompose_script": "skills.planner.quality_reviewer.qr_decompose",
        "verify_script": "skills.planner.quality_reviewer.qr_verify",
    },
    "impl-docs": {
        "workflow": "executor",
        "artifact": "plan.json",
        "decompose_script": "skills.planner.quality_reviewer.qr_decompose",
        "verify_script": "skills.planner.quality_reviewer.qr_verify",
    },
}


_VALID_WORKFLOWS = frozenset({"planner", "executor"})

_registries_validated = False


def validate_phase_registries() -> None:
    """Fail fast if the QR content registries don't cover exactly QR_PHASES.

    DECOMPOSE_CONTENT (decompose prompts) and VERIFIERS (verify classes) live in
    quality_reviewer/prompts/content.py, keyed by the same phase strings as
    QR_PHASES but defined far away. A phase added here but missing its content/
    verifier passes argparse (--phase choices come from QR_PHASES) and used to
    crash only when content.py was first imported mid-dispatch. Running the check
    on this eager path (get_phase_config -- hit at orchestrator routing and
    sub-agent content lookup, before the sub-agent is dispatched) surfaces the
    drift at the first QR phase lookup instead.

    The content import is function-local: phases.py stays import-clean (content.py
    imports phases.py, so a module-level import here would be circular), and a
    one-shot flag keeps repeat calls cheap. raise, not assert, so the guard
    survives `python -O`.
    """
    global _registries_validated
    if _registries_validated:
        return
    from skills.planner.quality_reviewer.prompts.content import DECOMPOSE_CONTENT, VERIFIERS
    from skills.planner.quality_reviewer.prompts.fix import FIX_CONTENT

    if not (set(DECOMPOSE_CONTENT) == set(VERIFIERS) == set(FIX_CONTENT) == set(QR_PHASES)):
        raise RuntimeError(
            "QR phase registries out of sync: "
            f"DECOMPOSE_CONTENT={sorted(DECOMPOSE_CONTENT)}, "
            f"VERIFIERS={sorted(VERIFIERS)}, FIX_CONTENT={sorted(FIX_CONTENT)}, "
            f"QR_PHASES={sorted(QR_PHASES)}"
        )
    for phase, cfg in QR_PHASES.items():
        if cfg.get("workflow") not in _VALID_WORKFLOWS:
            raise RuntimeError(
                f"QR phase {phase!r} has invalid workflow {cfg.get('workflow')!r}; "
                f"must be one of {sorted(_VALID_WORKFLOWS)}"
            )
    _registries_validated = True


def get_phase_config(phase: str) -> dict:
    """Single entry point for phase configuration.

    Understanding a phase's configuration requires reading only THIS file.
    Scripts import from here instead of hardcoding phase-specific values.

    Args:
        phase: Phase name (e.g., "plan-design", "impl-code")

    Returns:
        Phase configuration dict

    Raises:
        ValueError: If phase is unknown
    """
    validate_phase_registries()
    if phase not in QR_PHASES:
        valid = ", ".join(sorted(QR_PHASES.keys()))
        raise ValueError(f"Unknown QR phase: {phase}. Valid phases: {valid}")
    return QR_PHASES[phase]


def get_all_phases() -> list[str]:
    """Return list of all phase names."""
    return list(QR_PHASES.keys())


def is_execution_phase(phase: str) -> bool:
    """True for executor-workflow phases (impl-code, impl-docs).

    Execution-phase state dirs carry no planning context.json: the executor
    creates plan.json in step 1 but never context.json (that lives only in the
    planner's separate state dir). Plan-phase state dirs always have it -- the
    planner writes context.json in step 2 -- so its absence there is a real
    orchestrator bug, not a tolerated condition. Callers use this to decide
    whether a missing context.json should degrade gracefully (exec) or raise
    (plan). Unknown phases return False (strict), the conservative default.
    """
    cfg = QR_PHASES.get(phase)
    return bool(cfg and cfg.get("workflow") == "executor")
