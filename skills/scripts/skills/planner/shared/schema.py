"""Schema definitions and validation for planner state files.

Authoritative source for: context.json, plan.json, qr-{phase}.json schemas.
Pydantic is a required dependency (pydantic>=2.0 in pyproject.toml).
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from skills.planner.shared.qr.phases import get_all_phases

# QR item defaults for defensive access when reading malformed data
QA_ITEM_DEFAULTS = {
    "id": "unknown",
    "scope": "*",
    "check": "",
    "status": "TODO",
    "version": 1,
    "finding": None,
    "parent_id": None,
    "group_id": None,
    "severity": "SHOULD",  # Default for backwards compat with existing qr-{phase}.json files
}

# Valid severity values (per conventions/severity.md)
VALID_SEVERITIES = frozenset({"MUST", "SHOULD", "COULD"})

# Out-of-set tokens decompose agents occasionally emit meaning "maximum/blocking".
_SEVERITY_SYNONYMS = {"BLOCKER": "MUST", "CRITICAL": "MUST"}


def canonicalize_severity(v: object) -> str | None:
    """Map a raw severity token to a canonical tier, or None if unrecognized.

    Recognizes MUST/SHOULD/COULD (case-insensitive) plus the documented
    high-severity synonyms (BLOCKER/CRITICAL -> MUST, preserving blocking
    force). Returns None for genuinely-unknown tokens so callers pick policy:
    schema/routing default None -> SHOULD (lenient ingest, never aborts the
    run); the interactive update-item CLI rejects None (typo feedback).
    """
    if v is None:
        return None
    s = str(v).strip().upper()
    if not s:
        return None
    if s in VALID_SEVERITIES:
        return s
    return _SEVERITY_SYNONYMS.get(s)


PYDANTIC_AVAILABLE = True


# =============================================================================
# Context Schema (context.json)
# =============================================================================

if True:

    class Context(BaseModel):
        """Context captured in step 2 for sub-agent handover.

        Schema per INTENT.md lines 23-35. All fields are string arrays.
        Empty arrays acceptable; omitting fields is not.
        """

        task_spec: list[str]
        constraints: list[str]
        entry_points: list[str]
        rejected_alternatives: list[str]
        current_understanding: list[str]
        assumptions: list[str]
        invisible_knowledge: list[str]
        reference_docs: list[str]


# =============================================================================
# Plan Schema (plan.json)
# =============================================================================

if True:

    class Decision(BaseModel):
        """Architectural or design decision with CAS versioning."""

        id: str  # DL-001 format
        version: int = 1  # CAS optimistic locking: increment on update
        decision: str
        reasoning: str = Field(alias="reasoning_chain")  # premise -> implication -> conclusion

        class Config:
            populate_by_name = True

    class RejectedAlternative(BaseModel):
        """Alternative considered but rejected.

        id required by validate_refs() for error message clarity.
        """

        id: str
        alternative: str
        rejection_reason: str
        decision_ref: str  # DL-XXX cross-reference

    class Risk(BaseModel):
        """Identified risk with mitigation.

        id required by validate_refs() for error message clarity.
        """

        id: str
        risk: str
        mitigation: str
        anchor: str | None = None  # file:L###-L### line anchor
        decision_ref: str | None = None  # Optional DL-XXX cross-reference

    class PlanningContext(BaseModel):
        """Planning context container."""

        decisions: list[Decision] = Field(default_factory=list, alias="decision_log")
        rejected_alternatives: list[RejectedAlternative] = Field(default_factory=list)
        constraints: list[str] = Field(default_factory=list)  # INTENT.md line 67: string[]
        risks: list[Risk] = Field(default_factory=list, alias="known_risks")

        class Config:
            populate_by_name = True

    class InvisibleKnowledge(BaseModel):
        """Knowledge for future LLM sessions."""

        system: str = ""
        invariants: list[str] = Field(default_factory=list)
        tradeoffs: list[str] = Field(default_factory=list)

    class DiagramNode(BaseModel):
        """Node in a diagram graph."""

        id: str
        label: str
        type: str | None = None

    class DiagramEdge(BaseModel):
        """Edge connecting two nodes."""

        source: str
        target: str
        label: str
        protocol: str | None = None

    class DiagramGraph(BaseModel):
        """Architecture diagram as graph IR with optional ASCII render."""

        id: str
        type: Literal["architecture", "state", "sequence", "dataflow"]
        scope: str
        title: str
        nodes: list[DiagramNode] = Field(default_factory=list)
        edges: list[DiagramEdge] = Field(default_factory=list)
        ascii_render: str | None = None

    class CodeIntent(BaseModel):
        """Behavioral description for Developer to implement."""

        id: str  # CI-001 format
        version: int = 1  # CAS optimistic locking: increment on update
        file: str
        function: str | None = None
        behavior: str
        decision_refs: list[str] = Field(default_factory=list)  # DL-XXX cross-references

    class Milestone(BaseModel):
        """Single implementation milestone."""

        id: str  # M-001 format
        version: int = 1  # CAS optimistic locking: increment on update
        number: int
        name: str
        files: list[str]
        flags: list[str] = Field(default_factory=list)
        requirements: list[str] = Field(default_factory=list)
        acceptance_criteria: list[str] = Field(default_factory=list)
        tests: list[str] = Field(default_factory=list)  # Free-form test descriptions
        code_intents: list[CodeIntent] = Field(default_factory=list)
        is_documentation_only: bool = False
        delegated_to: str | None = None  # Agent name for delegation tracking

    class Wave(BaseModel):
        """Execution wave grouping milestones."""

        id: str  # W-001 format
        milestones: list[str]  # M-XXX IDs for parallel execution

    class Overview(BaseModel):
        """Plan overview."""

        problem: str
        approach: str

    class Plan(BaseModel):
        """Root plan.json schema.

        No schema_version field: state files are ephemeral (single planning session).
        Schema versioning adds complexity without benefit for short-lived artifacts.
        """

        plan_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
        created_at: str = Field(
            default_factory=lambda: datetime.now(UTC).isoformat()
        )

        overview: Overview
        planning_context: PlanningContext = Field(default_factory=PlanningContext)
        invisible_knowledge: InvisibleKnowledge = Field(default_factory=InvisibleKnowledge)
        milestones: list[Milestone] = Field(default_factory=list)
        waves: list[Wave] = Field(default_factory=list)
        diagram_graphs: list[DiagramGraph] = Field(default_factory=list)

        def get_milestone(self, mid: str) -> Milestone | None:
            for ms in self.milestones:
                if ms.id == mid:
                    return ms
            return None

        def get_intent(self, intent_id: str):
            for ms in self.milestones:
                for ci in ms.code_intents:
                    if ci.id == intent_id:
                        return ms, ci
            return None, None

        def get_decision(self, decision_id: str) -> Decision | None:
            for dl in self.planning_context.decisions:
                if dl.id == decision_id:
                    return dl
            return None

        def code_milestones(self) -> list[Milestone]:
            """Milestones that produce code (not is_documentation_only).

            Consumed by impl-code decompose (render_code_milestone_scope injects
            the explicit in-scope id list so the agent enumerates only these).
            Verify enforces the same subset via its `jq select(.is_documentation_only
            != true)` macro filter, and the executor needs no call because the
            wave invariant (validate_completeness forbids a doc-only milestone in
            any wave) already excludes them from developer dispatch.
            """
            return [ms for ms in self.milestones if not ms.is_documentation_only]

        @staticmethod
        def _next_numeric_id(existing_ids, prefix: str) -> str:
            """Max-based 'PREFIX-NNN', skipping non-canonical/non-ASCII-digit suffixes.

            Collision-safe after a pruned/hand-authored gap (see next_wave_id docstring).
            """
            nums = []
            for eid in existing_ids:
                head, _, suffix = eid.partition("-")
                if head == prefix and suffix.isascii() and suffix.isdigit():
                    nums.append(int(suffix))
            return f"{prefix}-{max(nums, default=0) + 1:03d}"

        def next_wave_id(self) -> str:
            """Next contiguous W-NNN id, skipping non-canonical existing ids.

            max()-based (not len()+1) because waves can be pruned (a doc-only
            toggle drops an emptied wave), so len()+1 could collide with a
            surviving id. A hand-authored/transcribed id like 'W1' or 'W-1a' is
            skipped rather than crashing int(w.id.split('-')[1]). The isascii()
            guard precedes isdigit() because str.isdigit() also accepts Unicode
            numerics (e.g. '²', '½', '๓') that int() then rejects with
            ValueError; restricting to ASCII keeps such a suffix skipped, not
            crashing.
            """
            return self._next_numeric_id((w.id for w in self.waves), "W")

        def next_milestone_id(self) -> str:
            return self._next_numeric_id((m.id for m in self.milestones), "M")

        def next_decision_id(self) -> str:
            return self._next_numeric_id((d.id for d in self.planning_context.decisions), "DL")

        def next_diagram_id(self) -> str:
            return self._next_numeric_id((g.id for g in self.diagram_graphs), "DIAG")

        def next_intent_id(self, ms) -> str:
            prefix = f"CI-{ms.id}"
            nums = []
            for ci in ms.code_intents:
                if ci.id.startswith(prefix + "-"):
                    suffix = ci.id[len(prefix) + 1 :]
                    if suffix.isascii() and suffix.isdigit():
                        nums.append(int(suffix))
            return f"{prefix}-{max(nums, default=0) + 1:03d}"

        def validate_diagram_edges(self, diagram_id: str) -> list[str]:
            """Validate edges for a specific diagram."""
            errors = []
            dg = next((d for d in self.diagram_graphs if d.id == diagram_id), None)
            if not dg:
                return [f"diagram {diagram_id} not found"]
            node_ids = {n.id for n in dg.nodes}
            for edge in dg.edges:
                if edge.source not in node_ids:
                    errors.append(f"diagram {dg.id} edge source '{edge.source}' not in nodes")
                if edge.target not in node_ids:
                    errors.append(f"diagram {dg.id} edge target '{edge.target}' not in nodes")
            return errors

        def validate_refs(self) -> list[str]:
            """Validate cross-references between entities.

            Returns error list (empty = valid). Prevents dangling references
            that would break navigation/traceability.
            """
            errors = []
            decision_ids = {dl.id for dl in self.planning_context.decisions}
            milestone_ids = {ms.id for ms in self.milestones}

            seen: set[str] = set()
            for ms in self.milestones:
                if ms.id in seen:
                    errors.append(f"duplicate milestone id '{ms.id}'")
                seen.add(ms.id)
            seen = set()
            for dl in self.planning_context.decisions:
                if dl.id in seen:
                    errors.append(f"duplicate decision id '{dl.id}'")
                seen.add(dl.id)
            seen = set()
            for ms in self.milestones:
                for ci in ms.code_intents:
                    if ci.id in seen:
                        errors.append(f"duplicate intent id '{ci.id}'")
                    seen.add(ci.id)
            seen = set()
            for dg in self.diagram_graphs:
                if dg.id in seen:
                    errors.append(f"duplicate diagram id '{dg.id}'")
                seen.add(dg.id)

            # Per-wave invariants in a single pass (id uniqueness, milestone refs,
            # intra-wave file overlap). Each milestone's file set is the union of its
            # declared files AND its code intents' target files, lexically normalized
            # AND case-folded once so the overlap check compares physical files, not
            # spellings -- a case-insensitive checkout (macOS/Windows) resolves
            # 'src/App.py' and 'src/app.py' to one file, so they must collide here too.
            # Intents are folded in because the executor dispatches one developer per
            # milestone, and that developer writes every file across the milestone's
            # code_intents[]: a missing or stale Milestone.files must not let two
            # milestones whose intent targets collide co-schedule in one wave (their two
            # developers would then race on the shared file mid-write).
            milestone_files = {
                ms.id: {
                    os.path.normpath(f).casefold()
                    for f in (*ms.files, *(ci.file for ci in ms.code_intents))
                    if f
                }
                for ms in self.milestones
            }
            seen_wave_ids: set[str] = set()
            for w in self.waves:
                # Wave ids are the executor's dispatch handles and the update-by-id
                # key; a duplicate id makes update-by-id edit only the first match and
                # silently strand the rest. set-wave only ever derives contiguous ids,
                # so the realistic source is a hand-authored/transcribed plan.json.
                if w.id in seen_wave_ids:
                    errors.append(f"duplicate wave id '{w.id}'")
                seen_wave_ids.add(w.id)

                # Waves are first-class milestone cross-references (executor IR): a
                # wave listing a nonexistent milestone would silently drop it from
                # execution.
                for mid in w.milestones:
                    if mid not in milestone_ids:
                        errors.append(f"wave {w.id} references unknown milestone '{mid}'")

                # Two milestones in one wave run as concurrent developer agents; if
                # they touch the same file they corrupt it mid-write (audit §2 leak 1).
                # Compare the normalized file sets built above (declared files plus
                # intent targets) so 'src/a.py' and './src/a.py' cannot evade the
                # check. Dedupe to distinct resolvable ids: a milestone listed twice
                # is a coverage issue (caught by validate_completeness), not a
                # self-overlap, and each pair is reported once. Dangling refs are
                # reported above.
                resolved = list(
                    dict.fromkeys(mid for mid in w.milestones if mid in milestone_files)
                )
                for i in range(len(resolved)):
                    for j in range(i + 1, len(resolved)):
                        a, b = resolved[i], resolved[j]
                        shared = milestone_files[a] & milestone_files[b]
                        if shared:
                            errors.append(
                                f"wave {w.id} co-schedules {a} and {b} which share "
                                f"file(s): {', '.join(sorted(shared))}"
                            )

            for ms in self.milestones:
                for ci in ms.code_intents:
                    for dref in ci.decision_refs:
                        if dref not in decision_ids:
                            errors.append(f"{ci.id}.decision_refs '{dref}' not in decisions")

            for ra in self.planning_context.rejected_alternatives:
                if ra.decision_ref not in decision_ids:
                    errors.append(f"{ra.id}.decision_ref '{ra.decision_ref}' not in decisions")
            for kr in self.planning_context.risks:
                if kr.decision_ref and kr.decision_ref not in decision_ids:
                    errors.append(f"{kr.id}.decision_ref '{kr.decision_ref}' not in decisions")

            for dg in self.diagram_graphs:
                errors.extend(self.validate_diagram_edges(dg.id))
                valid_scopes = {"overview", "invisible_knowledge"}
                if dg.scope in valid_scopes:
                    pass
                elif dg.scope.startswith("milestone:"):
                    mid = dg.scope.split(":", 1)[1]
                    if not self.get_milestone(mid):
                        errors.append(f"diagram {dg.id} scope references unknown milestone '{mid}'")
                else:
                    errors.append(
                        f"diagram {dg.id} has invalid scope '{dg.scope}' (must be 'overview', 'invisible_knowledge', or 'milestone:M-XXX')"
                    )

            return errors

        def validate_structural_executability(self) -> list[str]:
            """Wave/intent topology the executor requires (phase-independent).

            The structural invariant the executor enforces before dispatch: at
            least one milestone, the doc-only/code-intent exclusivity, and wave
            coverage (every code milestone in exactly one wave, no doc-only
            milestone in any wave). Excludes the plan-design prose check
            (overview.problem), which is not an executability concern -- so the
            executor calls this directly instead of borrowing the foreign
            "plan-design" phase name from validate_completeness.
            """
            errors = []
            if not self.milestones:
                errors.append("at least one milestone required")
            for ms in self.milestones:
                # Documentation-only milestones carry no code_intents; the
                # Technical Writer authors their docs at exec time (impl-docs).
                # The relationship is exclusive both ways so routing stays
                # unambiguous: doc-only => no code to implement; code => intent.
                if ms.is_documentation_only:
                    if ms.code_intents:
                        errors.append(
                            f"milestone {ms.id} is documentation-only but has code_intents"
                        )
                elif not ms.code_intents:
                    errors.append(f"milestone {ms.id} needs at least one code_intent")

            # Waves drive the executor's parallel developer dispatch, which covers
            # code milestones only; doc-only milestones route to exec-docs instead.
            # Require every code milestone in exactly one wave and no doc-only
            # milestone in any wave, so execution neither drops nor mis-routes one.
            # Completeness-only (not validate_refs): the planner saves partial plans
            # mid-build, where waves are authored after milestones.
            code_ids = {ms.id for ms in self.milestones if not ms.is_documentation_only}
            doc_only_ids = {ms.id for ms in self.milestones if ms.is_documentation_only}
            wave_counts: dict[str, int] = {}
            for w in self.waves:
                for mid in w.milestones:
                    wave_counts[mid] = wave_counts.get(mid, 0) + 1
            for mid in sorted(code_ids):
                count = wave_counts.get(mid, 0)
                if count == 0:
                    errors.append(f"milestone {mid} is not assigned to any wave")
                elif count > 1:
                    errors.append(f"milestone {mid} appears in multiple waves")
            for mid in sorted(doc_only_ids):
                if wave_counts.get(mid, 0) > 0:
                    errors.append(
                        f"documentation-only milestone {mid} must not appear in a "
                        f"wave (routes to exec-docs)"
                    )
            return errors

        def validate_completeness(self, phase: str) -> list[str]:
            """Phase-specific completeness validation.

            For plan-design this is the prose check (overview.problem) plus the
            phase-independent structural invariant in
            validate_structural_executability(). Other phases have no rule and
            return []. Error ordering is unchanged from the inlined version:
            overview.problem first, then the structural errors.
            """
            errors = []
            if phase == "plan-design":
                if not self.overview.problem:
                    errors.append("overview.problem required")
                errors.extend(self.validate_structural_executability())
            return errors


# =============================================================================
# QR Schema (qr-{phase}.json)
# =============================================================================

# Every character that can break a line: the C0 controls (0x00-0x1F, a superset of
# str.splitlines() line breaks), plus DEL (0x7F) and the Unicode line separators
# NEL (0x85), LS (0x2028), PS (0x2029).  Any of them in a field rendered into a
# PLAINTEXT agent prompt can forge a column-0 instruction line.  Identity fields
# reject them outright (QRItem._reject_control_chars); free-text sinks neutralize
# them while keeping a legitimate newline (qr/utils._fix_field_safe).  Single owner
# so the validator and neutralizer cannot drift.
LINE_FORGING_ORDS = frozenset(range(0x20)) | {0x7F, 0x85, 0x2028, 0x2029}


if True:

    class QRItem(BaseModel):
        """Single QR verification item."""

        id: str
        scope: str
        check: str
        status: str = "TODO"
        version: int = 1
        finding: str | None = None
        parent_id: str | None = None
        group_id: str | None = None
        severity: Literal["MUST", "SHOULD", "COULD"] = "SHOULD"

        @field_validator("severity", mode="before")
        @classmethod
        def _normalize_severity(cls, v: object) -> str:
            """Coerce severity on ingest; never aborts the run.

            Delegates to canonicalize_severity (MUST/SHOULD/COULD plus the
            high-severity synonyms BLOCKER/CRITICAL -> MUST). A strict Literal
            that aborted validate_state -- and with it the whole
            planner/executor run -- would be more brittle than the behaviour it
            guards, so a genuinely-unknown token defaults to SHOULD rather than
            raising.
            """
            return canonicalize_severity(v) or "SHOULD"

        @field_validator("id", "scope", mode="after")
        @classmethod
        def _reject_control_chars(cls, v: str, info: ValidationInfo) -> str:
            """Reject line-forging control/separator characters in id/scope.

            A decompose-authored newline (or any line break -- see
            LINE_FORGING_ORDS, incl. the Unicode separators NEL/LS/PS) here forges a
            line at column 0 of a PLAINTEXT agent prompt.  id reaches the parallel
            QR-verify dispatch (build_qr_verify_dispatch -> item_ids / qr_item_flags),
            where a forged '--- Agent N ---' line hijacks the verify fan-out; scope
            reaches the single-agent decompose/fix item listings (e.g. decompose's
            '{id} [scope={scope}]'), where it forges a fake item row.  id is also a
            lookup key (find_item / --qr-item), so it must be rejected, not silently
            rewritten.  check and finding are free-text fields neutralized at every
            sink (qr/utils._fix_field_safe and format_qr_item_for_verification);
            rejecting them here would regress legitimate multi-line content.
            validate_state runs this at step>1 entry, before any prompt renders, so
            a malformed item fails the run closed.
            """
            if any(ord(c) in LINE_FORGING_ORDS for c in v):
                raise ValueError(
                    f"{info.field_name} must be single-line plain text "
                    "(contains a control character)"
                )
            return v

    class QRFile(BaseModel):
        """qr-{phase}.json file structure."""

        phase: str
        iteration: int = 1
        # Fingerprint of the recorded FAIL set at the last RETRY iteration bump.
        # Idempotency key so a transient verify re-render doesn't double-count one
        # fix cycle (see prepare_verify_items / increment_qr_iteration).
        iteration_sig: str | None = None
        items: list[QRItem] = Field(default_factory=list)


# =============================================================================
# Validation Functions
# =============================================================================


class SchemaValidationError(Exception):
    """Raised when state files fail schema validation."""

    pass


# Schema registry: filename -> (model_class, post_validate_fn or None)
def _plan_post_validate(plan: Plan) -> list[str]:
    return plan.validate_refs()


_schema_registry: dict = {
    "context.json": (Context, None),
    "plan.json": (Plan, _plan_post_validate),
}


def validate_state(state_dir: str) -> tuple[Plan | None, dict[str, dict]]:
    """Validate all state files in state_dir; return the parsed plan.json and qr dicts.

    Raises SchemaValidationError on first validation failure.
    Call at start of every planner/executor step and after CLI mutations.

    Returns the validated Plan when plan.json is present, so a caller that needs
    the plan (the executor's structural-executability gate) reuses this parse
    instead of re-reading the file; returns None when plan.json is absent.
    Callers that only validate may ignore the return value.

    Also returns a dict of phase -> raw qr-{phase}.json dict, already parsed
    and validated, so the orchestrator's gate path avoids a second load.
    """
    state_path = Path(state_dir)
    plan: Plan | None = None

    for filename, (model, post_validate) in _schema_registry.items():
        path = state_path / filename
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            obj = model.model_validate(data)
            if post_validate:
                errors = post_validate(obj)
                if errors:
                    raise SchemaValidationError(f"{filename}: {errors}")
            if filename == "plan.json":
                plan = obj
        except SchemaValidationError:
            raise
        except Exception as e:
            raise SchemaValidationError(f"{filename}: {e}") from e

    # Validate only the canonical qr-{phase}.json files. A decompose agent can
    # leave non-canonical scratch files (e.g. qr-items.json, qr-items-draft.json)
    # in the state dir; a bare `qr-*.json` glob validated those as QRFile dicts
    # and aborted the whole run on a list-shaped scratch file (audit §3 #3, field
    # evidence ab1dc60a: "Input should be a valid dictionary ... input_type=list").
    # Restricting to the known phases ignores any non-canonical file by construction.
    qr_states: dict[str, dict] = {}
    for phase in get_all_phases():
        path = state_path / f"qr-{phase}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            qr_file = QRFile.model_validate(data)
            qr_states[phase] = qr_file.model_dump(mode="json")
        except json.JSONDecodeError:
            # Truncated/partial canonical file from a non-atomic decompose Write, or
            # raw control chars (json rejects both). Remove the corrupt file so the
            # next decompose recreates it; a bare existence check would skip it and
            # the verify→decompose route-back loop would cycle indefinitely. A
            # *parseable* but schema-invalid file (e.g. a forged item) is instead
            # rejected by model_validate -> SchemaValidationError -- the security
            # boundary before the verify fan-out renders it.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        except Exception as e:
            raise SchemaValidationError(f"{path.name}: {e}") from e

    return plan, qr_states


def plan_completeness_errors(
    state_dir: str,
    phase: str,
    suppress_if_no_milestones: bool = False,
    plan: Plan | None = None,
) -> list[str]:
    """Load plan.json and return its validate_completeness errors ([] when N/A).

    Shared by the QR gate (which vetoes a QR-pass on a structurally incomplete
    plan) and the architect router (which surfaces the same gaps to the
    re-dispatched architect), so the two read the contract from one place.
    Tolerant of a missing/unparseable plan.json -- validate_state already gates
    schema validity at step entry, so this layers only the phase completeness
    check; returns [] for phases without a completeness rule (impl-code/docs).

    The gate veto and the executor's step>1 guard call this to fail CLOSED: an
    empty/partial plan ("at least one milestone required") is a real completeness
    error they must reject. Only the architect router opts into
    suppress_if_no_milestones=True, because at step 1 an empty skeleton is
    genuine first-time execution (not a repairable gap) and it prints
    "First-time execution" rather than surfacing spurious completeness errors.

    Pass `plan` when the caller already parsed plan.json (the gate threads its
    validate_state parse down) to skip the redundant disk read+parse; when None,
    this loads plan.json itself, tolerating a missing/unparseable file as above.
    """
    if not (state_dir and phase):
        return []
    if plan is None:
        path = Path(state_dir) / "plan.json"
        if not path.exists():
            return []
        try:
            plan = Plan.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return []
    if suppress_if_no_milestones and not plan.milestones:
        return []
    return plan.validate_completeness(phase)


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "PYDANTIC_AVAILABLE",
    # QR constants
    "QA_ITEM_DEFAULTS",
    "CodeIntent",
    # Models
    "Context",
    "Decision",
    "DiagramEdge",
    "DiagramGraph",
    "DiagramNode",
    "InvisibleKnowledge",
    "Milestone",
    "Overview",
    "Plan",
    "PlanningContext",
    "QRFile",
    "QRItem",
    "RejectedAlternative",
    "Risk",
    "SchemaValidationError",
    "Wave",
    "canonicalize_severity",
    "plan_completeness_errors",
    "validate_state",
]
