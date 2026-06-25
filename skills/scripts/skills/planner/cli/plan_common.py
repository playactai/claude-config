"""Shared plan-CLI primitives for plan.py and plan_commands.py.

Both the live CLI (plan.py, ``Command`` classes) and the batch-RPC library
(plan_commands.py, plain functions) run the same validate -> atomic-write cycle
over plan.json and apply the same path/toggle guards before persisting. This
module holds the pieces that were hand-copied across them -- and drifted, so a
guard fixed on one path stayed broken on the other -- the relative-path guard,
the wave doc-only guard, the validate-then-write core, and the
documentation-only toggle. Keeping the one
copy here is what stops the two entry points diverging again. Each caller keeps
its own failure mode (plan.py's error_exit vs plan_commands' raise) by catching
the ValueError validate_relpath raises, which is why the guard signals via
exception rather than either caller's exit path.
"""

from __future__ import annotations

import os
from pathlib import Path

from skills.lib.io import atomic_write_text

from ..shared.schema import Milestone, Plan, SchemaValidationError


def validate_relpath(path: str, context: str) -> str:
    """Normalize a path, reject absolute / parent-relative spellings, return the normalized form.

    validate_refs (schema.py) compares os.path.normpath strings to keep two
    file-sharing milestones out of one wave; storing the value returned here is
    what makes that comparison see matching spellings. Normalizing -- not merely
    stripping -- closes the two evasions the strip-only guard missed: leading
    whitespace (' src/a.py' never matched 'src/a.py') and an embedded '..'
    ('a/../../shared.py' has no leading '..' yet collapses to the out-of-tree
    '../shared.py'). Path components that equal '..' are rejected regardless of
    position, so '../x' and 'a/../../b.py' both fail, while a filename that
    merely contains '..' (like '..config.py') is correctly accepted. A spelling
    that collapses to the current directory ('.', './', 'a/..', or a
    whitespace-only value -- all normalize to '.') is rejected too: it names no
    file, so storing it would seed a nonsense milestone/intent target.

    Pure: raises ValueError so each caller wraps it in its own failure mode.
    """
    if not path:
        return path
    normalized = os.path.normpath(path.strip())
    if os.path.isabs(normalized):
        raise ValueError(f"Absolute path not allowed in {context}: {normalized}")
    if os.pardir in normalized.split(os.sep):
        raise ValueError(f"Parent-relative path not allowed in {context}: {normalized}")
    if normalized == ".":
        raise ValueError(f"Path must name a file, not the current directory, in {context}: {path!r}")
    return normalized


# The param names whose RPC value is tokenized by parse_csv. dispatch._normalize_params
# leaves these as lists so parse_csv (which accepts str|list) owns their shape; every
# other param is scalar. Adding a parse_csv-backed param REQUIRES adding it here:
# test_csv_param_names_matches_parse_csv_call_sites introspects plan_commands' parse_csv
# call sites and fails the moment this frozenset drifts from them.
CSV_PARAM_NAMES = frozenset({
    "files", "flags", "requirements", "acceptance_criteria", "tests",  # set_milestone
    "decision_refs",                                                    # set_intent
    "milestones",                                                       # set_wave
})


# Fields each dual create/update command requires when CREATING (no id). These
# functions mark every field optional (default None) so one function serves both
# create and update -- which makes the signature-derived "required" set empty and
# misleading for create. This declarative map is the single source the
# discoverability surfaces (dispatch.list_methods, the architect method catalog
# prose) derive create-requiredness from, so they cannot teach a create shape the
# runtime create-branch guards don't enforce. The guards keep their own ordering and
# messages (procedural rules); test_create_required_matches_runtime_guards pins them
# to this map.
CREATE_REQUIRED: dict[str, list[str]] = {
    "set-decision": ["decision", "reasoning"],
    "set-milestone": ["name"],
    "set-intent": ["milestone", "file", "behavior"],
    "set-wave": ["milestones"],
}


def parse_csv(value: str | list[str] | None) -> list[str]:
    """Split a comma-separated CLI value into stripped, non-empty tokens.

    Shared by plan.py and plan_commands.py so the two CLI mirrors tokenize a
    --files/--flags/... value identically: an empty token (a trailing or doubled
    comma, or a whitespace-only value) is dropped on BOTH paths rather than kept
    as a "" entry by the live CLI while the RPC filtered it.

    Accepts a JSON array (list[str]) from batch/RPC callers as well as the
    comma-separated string argparse yields — JSON arrays are the idiomatic form
    for lists, and rejecting them with a cryptic ``'list' object has no
    attribute 'split'`` made the batch surface hostile to the exact shape the
    catalog's bare key names invite.

    Raises ValueError when a list element is not a string (e.g. a bare number
    like 123) so the caller sees a clear error rather than a silent str()-coercion
    that masks the mis-typed RPC value.
    """
    # Only None short-circuits to [] here. Empty collections fall through but still
    # yield []: "" via the str branch, [] via the list branch. Falsy non-None
    # SCALARS (False, 0, 0.0, {}) fall through to the type checks and raise a clear
    # ValueError, instead of silently clearing persisted data.
    if value is None:
        return []
    if isinstance(value, list):
        if not all(isinstance(v, str) for v in value):
            raise ValueError(f"expected a list of strings, got {value!r}")
        return [v.strip() for v in value if v.strip()]
    if not isinstance(value, str):
        raise ValueError(
            f"expected a string or list of strings, got {type(value).__name__} {value!r}"
        )
    return [v.strip() for v in value.split(",") if v.strip()]


def reject_doc_only_in_wave(plan: Plan, milestone_ids: list[str]) -> None:
    """Reject adding documentation-only milestones to an execution wave.

    Doc-only milestones route to exec-docs, never the executor's parallel waves.
    Shared by plan.py's SetWaveCommand and plan_commands' set_wave so the live CLI
    and the batch/RPC twin enforce it identically (the executor's
    validate_structural_executability rejects it later, but failing at write time
    is the point). Pure: raises ValueError so each caller wraps it in its own
    failure mode (error_exit vs re-raise), matching validate_relpath.
    """
    doc_only_ids = {ms.id for ms in plan.milestones if ms.is_documentation_only}
    bad = [mid for mid in milestone_ids if mid in doc_only_ids]
    if bad:
        raise ValueError(
            f"documentation-only milestone(s) {bad} cannot be added to a wave (they route to exec-docs)"
        )


def write_plan(plan_path: Path, plan: Plan) -> None:
    """Validate cross-references, then atomically persist plan.json.

    The validate-then-write core shared by plan.py's module-level save_plan and
    plan_commands' PlanContext.save_plan: validate_refs BEFORE the write, so a
    schema-invalid mutation (e.g. a wave co-scheduling two milestones that share
    a file) never reaches disk -- no bad write, no rollback -- and an unrelated
    malformed qr-{phase}.json in the same state dir cannot fail a valid plan
    mutation. The batch path layers its own transaction snapshot on top.
    """
    errors = plan.validate_refs()
    if errors:
        raise SchemaValidationError(f"plan.json: {errors}")
    atomic_write_text(plan_path, plan.model_dump_json(indent=2))


def apply_documentation_only_toggle(
    plan: Plan, ms: Milestone, documentation_only: bool
) -> tuple[int, int, str | None, list[str]]:
    """Apply a documentation_only flip to ms; return (cleared_intents, dropped_from_waves, warning, missing).

    The complete toggle both CLI mirrors must run identically. The forward flip
    (code -> doc-only) clears the milestone's code_intents and drops it from
    every wave (pruning emptied waves), because doc-only milestones route to
    exec-docs, not the executor's parallel dispatch. The reverse flip
    (doc-only -> code) only flips the flag -- it does NOT re-add the intents or
    wave the forward flip stripped -- so it returns a non-fatal warning naming
    what the now-code milestone is still missing (code_intents and/or a wave),
    which validate_completeness will reject until the plan is re-authored.
    The structured `missing` list holds machine-actionable tokens (\"code_intents\",
    \"wave\") alongside the human-readable warning.

    Callers keep the `documentation_only is not None` guard and own how they
    surface the warning (RPC result dict vs CLI stderr).
    """
    cleared_intents = 0
    dropped_from_waves = 0
    toggle_off_warning = None
    missing: list[str] = []
    ms.is_documentation_only = documentation_only
    if documentation_only and ms.code_intents:
        cleared_intents = len(ms.code_intents)
        ms.code_intents = []
    # Doc-only milestones route to exec-docs, not the executor's waves.
    if documentation_only:
        for w in plan.waves:
            before = len(w.milestones)
            w.milestones = [m for m in w.milestones if m != ms.id]
            dropped_from_waves += before - len(w.milestones)
        # Prune emptied waves so the plan doesn't accumulate dead waves
        # across repeated toggles.
        plan.waves = [w for w in plan.waves if w.milestones]
    # The reverse toggle (doc-only -> code) only flips the flag; it does NOT
    # re-add the code_intents or wave the forward toggle stripped, leaving a
    # code milestone that validate_completeness rejects -- yet the command
    # still returns success. Surface a non-fatal warning so the gap is visible
    # (mirrors set_intent pointing users at --no-documentation-only).
    if documentation_only is False:
        in_wave = any(ms.id in w.milestones for w in plan.waves)
        missing_human = []
        if not ms.code_intents:
            missing.append("code_intents")
            missing_human.append(f"code_intents (set-intent --milestone {ms.id} ...)")
        if not in_wave:
            missing.append("wave")
            missing_human.append("a wave assignment")
        if missing_human:
            toggle_off_warning = (
                f"milestone {ms.id} is now a code milestone but is missing "
                f"{' and '.join(missing_human)}; validate_completeness will reject "
                f"the plan until re-authored."
            )
    return cleared_intents, dropped_from_waves, toggle_off_warning, missing
