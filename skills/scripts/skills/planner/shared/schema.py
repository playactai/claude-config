"""Schema definitions and validation for planner state files.

Authoritative source for: context.json, plan.json, qr-{phase}.json schemas.
Pydantic is a required dependency (pydantic>=2.0 in pyproject.toml).
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

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

# Canonical field names for QR items
QA_ITEM_REQUIRED_FIELDS = frozenset({"id", "scope", "check", "status", "version"})
QA_ITEM_OPTIONAL_FIELDS = frozenset({"finding", "parent_id", "group_id", "severity"})
QA_ITEM_ALL_FIELDS = QA_ITEM_REQUIRED_FIELDS | QA_ITEM_OPTIONAL_FIELDS

# Valid severity values (per conventions/severity.md)
VALID_SEVERITIES = frozenset({"MUST", "SHOULD", "COULD"})

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
        frozen_at: str | None = None  # Timestamp when plan execution began

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

            # Per-wave invariants in a single pass (id uniqueness, milestone refs,
            # intra-wave file overlap). Each milestone's file set is the union of its
            # declared files AND its code intents' target files, lexically normalized
            # once so the overlap check compares physical files, not spellings. Intents
            # are folded in because the executor dispatches one developer per milestone,
            # and that developer writes every file across the milestone's code_intents[]:
            # a missing or stale Milestone.files must not let two milestones whose intent
            # targets collide co-schedule in one wave (their two developers would then
            # race on the shared file mid-write).
            milestone_files = {
                ms.id: {
                    os.path.normpath(f)
                    for f in (*ms.files, *(ci.file for ci in ms.code_intents))
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

        def validate_completeness(self, phase: str) -> list[str]:
            """Phase-specific completeness validation."""
            errors = []
            if phase == "plan-design":
                if not self.overview.problem:
                    errors.append("overview.problem required")
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


# =============================================================================
# QR Schema (qr-{phase}.json)
# =============================================================================

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
            """Case-fold and coerce severity on ingest.

            Decompose agents occasionally emit lower-case ("must") or
            out-of-set ("BLOCKER") severities. The routing layer
            (by_blocking_severity) already tolerates unknowns by treating them
            as non-blocking, so a strict Literal that aborts validate_state --
            and with it the whole planner/executor run -- is more brittle than
            the behaviour it guards. Normalise to the canonical set, defaulting
            unknown/empty values to SHOULD.
            """
            if v is None:
                return "SHOULD"
            s = str(v).strip().upper()
            return s if s in VALID_SEVERITIES else "SHOULD"

    class QRFile(BaseModel):
        """qr-{phase}.json file structure."""

        phase: str
        iteration: int = 1
        items: list[QRItem] = Field(default_factory=list)


# =============================================================================
# QR Schema Helpers (moved from shared/qr/schema.py)
# =============================================================================

QA_ITEM_SCHEMA_TEMPLATE = """{
  "id": "{id_example}",
  "scope": "*" or "file:path:lines",
  "check": "Description of what was checked",
  "status": "TODO",
  "version": 1,
  "finding": null
}"""


def get_qa_state_schema_example(phase: str, id_prefix: str = "qa") -> str:
    """Generate schema example for prompts."""
    return f'''{{
  "phase": "{phase}",
  "items": [
    {QA_ITEM_SCHEMA_TEMPLATE.format(id_example=f"{id_prefix}-001")}
  ]
}}'''


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


def validate_state(state_dir: str) -> None:
    """Validate all state files in state_dir.

    Raises SchemaValidationError on first validation failure.
    Call at start of every planner/executor step and after CLI mutations.
    """
    state_path = Path(state_dir)

    for filename, (model, post_validate) in _schema_registry.items():
        path = state_path / filename
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            obj = model.model_validate(data)
            if post_validate:
                errors = post_validate(obj)
                if errors:
                    raise SchemaValidationError(f"{filename}: {errors}")
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
    for phase in get_all_phases():
        path = state_path / f"qr-{phase}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            QRFile.model_validate(data)
        except Exception as e:
            raise SchemaValidationError(f"{path.name}: {e}") from e


def plan_completeness_errors(state_dir: str, phase: str) -> list[str]:
    """Load plan.json and return its validate_completeness errors ([] when N/A).

    Shared by the QR gate (which vetoes a QR-pass on a structurally incomplete
    plan) and the architect router (which surfaces the same gaps to the
    re-dispatched architect), so the two read the contract from one place.
    Tolerant of a missing/unparseable plan.json -- validate_state already gates
    schema validity at step entry, so this layers only the phase completeness
    check; returns [] for phases without a completeness rule (impl-code/docs).
    """
    if not (state_dir and phase):
        return []
    path = Path(state_dir) / "plan.json"
    if not path.exists():
        return []
    try:
        plan = Plan.model_validate(json.loads(path.read_text()))
    except Exception:
        return []
    return plan.validate_completeness(phase)


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "PYDANTIC_AVAILABLE",
    "QA_ITEM_ALL_FIELDS",
    # QR constants
    "QA_ITEM_DEFAULTS",
    "QA_ITEM_OPTIONAL_FIELDS",
    "QA_ITEM_REQUIRED_FIELDS",
    "QA_ITEM_SCHEMA_TEMPLATE",
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
    "get_qa_state_schema_example",
    "plan_completeness_errors",
    "validate_state",
]
