"""Plan manipulation commands as plain functions.

Each public function with 'ctx' as first param is auto-discovered as RPC method.
Function names use underscores, converted to hyphens for method names.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from .plan_common import (
    apply_documentation_only_toggle,
    parse_csv,
    reject_doc_only_in_wave,
    validate_relpath,
    write_plan,
)

if TYPE_CHECKING:
    from ..shared.schema import Plan


@functools.cache
def _get_schema():
    """Lazy import to avoid circular deps."""
    from ..shared.schema import (
        CodeIntent,
        Decision,
        DiagramEdge,
        DiagramGraph,
        DiagramNode,
        Milestone,
        Overview,
        Plan,
        Wave,
    )

    return {
        "Plan": Plan,
        "Overview": Overview,
        "Milestone": Milestone,
        "CodeIntent": CodeIntent,
        "Decision": Decision,
        "DiagramGraph": DiagramGraph,
        "DiagramNode": DiagramNode,
        "DiagramEdge": DiagramEdge,
        "Wave": Wave,
    }


@dataclass
class PlanContext:
    """Context passed to all plan commands.

    No batch_lock: plan design is single-author (no concurrent writers), so the
    dispatcher's nullcontext() default is correct. The batch-cache hooks
    (begin_batch / flush_batch / end_batch) are independent of cross-process
    locking, which QRContext provides via qr_write_lock.
    """

    state_dir: Path
    _batch: Plan | None = None
    _batch_dirty: bool = False

    def plan_path(self) -> Path:
        return self.state_dir / "plan.json"

    def state_file(self) -> Path:
        """Single mutable state file (used by batch snapshot/rollback)."""
        return self.plan_path()

    def begin_batch(self) -> None:
        try:
            self._batch = self.load_plan()
        except FileNotFoundError:
            self._batch = None
        self._batch_dirty = False

    def end_batch(self) -> None:
        self._batch = None
        self._batch_dirty = False

    def load_plan(self) -> Plan:
        if self._batch is not None:
            return self._batch
        schema = _get_schema()
        path = self.plan_path()
        if not path.exists():
            raise FileNotFoundError(f"plan.json not found at {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return schema["Plan"].model_validate(data)

    def save_plan(self, plan: Plan) -> None:
        """Validate the in-memory plan, then atomically persist it.

        Single-call path: delegate to plan_common.write_plan (shared with
        cli.plan.save_plan) so the validate-refs-before-write core lives in one
        place and the two CLI mirrors cannot drift -- validation happens once,
        there.

        Batch path: validate here (per save) so a bad mutation is attributed to
        its own request, then cache it; flush_batch() performs the single disk
        write. Validating only in this branch avoids double-validating every
        single-call save (write_plan would validate again).
        """
        if self._batch is not None:
            errors = plan.validate_refs()
            if errors:
                from ..shared.schema import SchemaValidationError
                raise SchemaValidationError(f"plan.json: {errors}")
            self._batch = plan
            self._batch_dirty = True
            return
        write_plan(self.plan_path(), plan)

    def flush_batch(self) -> None:
        # Persist once, and only if a command actually mutated the cache -- a
        # read-only batch must not rewrite (and normalize) the state file.
        if self._batch is not None and self._batch_dirty:
            write_plan(self.plan_path(), self._batch)
        self._batch = None
        self._batch_dirty = False


def _check_version(entity, provided: int | None, entity_id: str) -> None:
    """CAS version check. Raises if mismatch."""
    if provided is None:
        return
    current = getattr(entity, "version", 1)
    if provided != current:
        raise ValueError(
            f"Version mismatch for {entity_id}: provided {provided}, current {current}. "
            f"Re-read entity and retry with version {current}"
        )


def _bump_version(entity) -> None:
    """Increment entity version."""
    if hasattr(entity, "version"):
        entity.version += 1


# -----------------------------------------------------------------------------
# Initialization
# -----------------------------------------------------------------------------


def init(ctx: PlanContext, task: str, title: str = "Untitled Plan") -> dict:
    """Initialize new plan.json with task description."""
    schema = _get_schema()
    path = ctx.plan_path()

    if path.exists():
        raise FileExistsError(f"plan.json already exists at {path}")

    plan = schema["Plan"](overview=schema["Overview"](problem=task, approach=""))
    ctx.save_plan(plan)

    return {"id": plan.plan_id, "version": 1, "operation": "created"}


# -----------------------------------------------------------------------------
# Architect Phase
# -----------------------------------------------------------------------------


def set_milestone(
    ctx: PlanContext,
    name: str | None = None,
    id: str | None = None,
    version: int | None = None,
    files: str | None = None,
    flags: str | None = None,
    requirements: str | None = None,
    acceptance_criteria: str | None = None,
    tests: str | None = None,
    documentation_only: bool | None = None,
) -> dict:
    """Create or update milestone."""
    schema = _get_schema()
    plan = ctx.load_plan()

    files_list = parse_csv(files)
    flags_list = parse_csv(flags)
    requirements_list = parse_csv(requirements)
    acceptance_list = parse_csv(acceptance_criteria)
    tests_list = parse_csv(tests)

    if files_list:
        files_list = [validate_relpath(f, "set-milestone files") for f in files_list]

    if id:
        # UPDATE
        ms = plan.get_milestone(id)
        if not ms:
            ids = [m.id for m in plan.milestones]
            raise ValueError(f"Milestone {id} not found. Valid: {ids}")

        _check_version(ms, version, id)

        if name:
            ms.name = name
        if files_list:
            ms.files = files_list
        if flags_list:
            ms.flags = flags_list
        if requirements_list:
            ms.requirements = requirements_list
        if acceptance_list:
            ms.acceptance_criteria = acceptance_list
        if tests_list:
            ms.tests = tests_list
        # documentation_only: None = leave as-is; True/False applied by the shared
        # toggle (clears intents + drops the milestone from waves on the forward
        # flip, warns on the reverse flip). See plan_common.apply_documentation_only_toggle.
        cleared_intents = 0
        dropped_from_waves = 0
        toggle_off_warning = None
        toggle_missing: list[str] = []
        if documentation_only is not None:
            cleared_intents, dropped_from_waves, toggle_off_warning, toggle_missing = (
                apply_documentation_only_toggle(plan, ms, documentation_only)
            )

        _bump_version(ms)
        ctx.save_plan(plan)
        result = {"id": ms.id, "version": ms.version, "operation": "updated"}
        if cleared_intents:
            result["cleared_code_intents"] = cleared_intents
        if dropped_from_waves:
            result["dropped_from_waves"] = dropped_from_waves
        if toggle_off_warning:
            result["warning"] = toggle_off_warning
        if toggle_missing:
            result["missing"] = toggle_missing
        return result
    else:
        # CREATE
        if version is not None:
            raise ValueError("version is only valid for updates (provide id to update)")
        if not name:
            raise ValueError("name required for create")

        mid = plan.next_milestone_id()
        number = int(mid.rsplit("-", 1)[1])

        ms = schema["Milestone"](
            id=mid,
            version=1,
            number=number,
            name=name,
            files=files_list,
            flags=flags_list,
            requirements=requirements_list,
            acceptance_criteria=acceptance_list,
            tests=tests_list,
            is_documentation_only=bool(documentation_only),
        )
        plan.milestones.append(ms)
        ctx.save_plan(plan)
        return {"id": mid, "version": 1, "operation": "created"}


def set_intent(
    ctx: PlanContext,
    milestone: str,
    file: str | None = None,
    behavior: str | None = None,
    id: str | None = None,
    version: int | None = None,
    function: str | None = None,
    decision_refs: str | None = None,
) -> dict:
    """Create or update code intent."""
    schema = _get_schema()
    plan = ctx.load_plan()

    ms = plan.get_milestone(milestone)
    if not ms:
        ids = [m.id for m in plan.milestones]
        raise ValueError(f"Milestone {milestone} not found. Valid: {ids}")

    # Doc-only milestones carry no code_intents (exclusive relationship -- see
    # Plan.validate_completeness). Reject so the plan can't be wedged invalid.
    if ms.is_documentation_only:
        raise ValueError(
            f"milestone {milestone} is documentation-only; set documentation_only=false "
            f"on it via set-milestone before adding code intents"
        )

    if file:
        file = validate_relpath(file, "set-intent file")

    refs_list = parse_csv(decision_refs)
    for ref in refs_list:
        if not plan.get_decision(ref):
            raise ValueError(f"Decision {ref} not found")

    if id:
        # UPDATE
        _, ci = plan.get_intent(id)
        if not ci:
            all_intents = [c.id for m in plan.milestones for c in m.code_intents]
            raise ValueError(f"Intent {id} not found. Valid: {all_intents}")

        _check_version(ci, version, id)

        if file:
            ci.file = file
        if function is not None:
            ci.function = function if function else None
        if behavior:
            ci.behavior = behavior
        if refs_list:
            ci.decision_refs = refs_list

        _bump_version(ci)
        ctx.save_plan(plan)
        return {"id": ci.id, "version": ci.version, "operation": "updated"}
    else:
        # CREATE
        if version is not None:
            raise ValueError("version is only valid for updates (provide id to update)")
        if not file or not behavior:
            raise ValueError("file and behavior required for create")

        cid = plan.next_intent_id(ms)

        ci = schema["CodeIntent"](
            id=cid,
            version=1,
            file=file,
            function=function,
            behavior=behavior,
            decision_refs=refs_list,
        )
        ms.code_intents.append(ci)
        ctx.save_plan(plan)
        return {"id": cid, "version": 1, "operation": "created"}


def set_decision(
    ctx: PlanContext,
    decision: str | None = None,
    reasoning: str | None = None,
    id: str | None = None,
    version: int | None = None,
) -> dict:
    """Create or update decision."""
    schema = _get_schema()
    plan = ctx.load_plan()

    if id:
        # UPDATE
        dl = plan.get_decision(id)
        if not dl:
            dids = [d.id for d in plan.planning_context.decisions]
            raise ValueError(f"Decision {id} not found. Valid: {dids}")

        _check_version(dl, version, id)

        if decision:
            dl.decision = decision
        if reasoning:
            dl.reasoning = reasoning

        _bump_version(dl)
        ctx.save_plan(plan)
        return {"id": dl.id, "version": dl.version, "operation": "updated"}
    else:
        # CREATE
        if version is not None:
            raise ValueError("version is only valid for updates (provide id to update)")
        if not decision or not reasoning:
            raise ValueError("decision and reasoning required for create")

        did = plan.next_decision_id()

        dl = schema["Decision"](id=did, version=1, decision=decision, reasoning=reasoning)
        plan.planning_context.decisions.append(dl)
        ctx.save_plan(plan)
        return {"id": did, "version": 1, "operation": "created"}


def set_diagram(ctx: PlanContext, type: str, scope: str, title: str, id: str | None = None) -> dict:
    """Create or update diagram graph."""
    schema = _get_schema()
    plan = ctx.load_plan()

    if id:
        dg = next((d for d in plan.diagram_graphs if d.id == id), None)
        if not dg:
            raise ValueError(f"Diagram {id} not found")
        valid_types = ("architecture", "state", "sequence", "dataflow")
        if type not in valid_types:
            raise ValueError(f"Invalid diagram type: {type}")
        dg.type = cast(Literal["architecture", "state", "sequence", "dataflow"], type)
        dg.scope = scope
        dg.title = title
        operation = "updated"
    else:
        new_id = plan.next_diagram_id()
        dg = schema["DiagramGraph"](id=new_id, type=type, scope=scope, title=title)
        plan.diagram_graphs.append(dg)
        id = new_id
        operation = "created"

    ctx.save_plan(plan)
    return {"id": id, "version": 1, "operation": operation}


def add_diagram_node(
    ctx: PlanContext, diagram: str, node_id: str, label: str, type: str | None = None
) -> dict:
    """Add node to diagram."""
    schema = _get_schema()
    plan = ctx.load_plan()

    dg = next((d for d in plan.diagram_graphs if d.id == diagram), None)
    if not dg:
        raise ValueError(f"Diagram {diagram} not found")

    if any(n.id == node_id for n in dg.nodes):
        raise ValueError(f"Node {node_id} already exists in {diagram}")

    node = schema["DiagramNode"](id=node_id, label=label, type=type)
    dg.nodes.append(node)
    ctx.save_plan(plan)

    return {"id": node_id, "diagram": diagram, "operation": "created"}


def add_diagram_edge(
    ctx: PlanContext,
    diagram: str,
    source: str,
    target: str,
    label: str,
    protocol: str | None = None,
) -> dict:
    """Add edge to diagram."""
    schema = _get_schema()
    plan = ctx.load_plan()

    dg = next((d for d in plan.diagram_graphs if d.id == diagram), None)
    if not dg:
        raise ValueError(f"Diagram {diagram} not found")

    edge = schema["DiagramEdge"](source=source, target=target, label=label, protocol=protocol)
    dg.edges.append(edge)

    errors = plan.validate_diagram_edges(diagram)
    if errors:
        dg.edges.pop()
        raise ValueError(errors[0])

    ctx.save_plan(plan)
    return {"source": source, "target": target, "diagram": diagram, "operation": "created"}


# -----------------------------------------------------------------------------
# Diagram Render
# -----------------------------------------------------------------------------


def set_diagram_render(ctx: PlanContext, diagram: str, content_file: str) -> dict:
    """Set ASCII render for diagram."""
    plan = ctx.load_plan()

    dg = next((d for d in plan.diagram_graphs if d.id == diagram), None)
    if not dg:
        raise ValueError(f"Diagram {diagram} not found")

    content_path = Path(content_file)
    if not content_path.exists():
        raise FileNotFoundError(f"Content file not found: {content_file}")

    dg.ascii_render = content_path.read_text(encoding="utf-8")
    ctx.save_plan(plan)

    return {"diagram": diagram, "operation": "updated"}


def set_wave(
    ctx: PlanContext,
    milestones: str,
    id: str | None = None,
) -> dict:
    """Create or update an execution wave (milestones that run in parallel).

    `milestones` is required (it may be the empty string to blank a wave), matching
    the `plan set-wave` CLI's required --milestones flag -- a batch call omitting it
    errors instead of silently creating an empty wave the CLI would reject.

    No CAS (Wave has no version field), so the result omits `version`: save_plan
    validates cross-references, rejecting a wave that co-schedules two milestones
    sharing a file.
    """
    schema = _get_schema()
    plan = ctx.load_plan()

    milestones_list = parse_csv(milestones)

    # Doc-only milestones route to exec-docs and must never enter a wave; the
    # executor's validate_structural_executability rejects it later, but fail at
    # write time. Shared with SetWaveCommand.run via plan_common.
    reject_doc_only_in_wave(plan, milestones_list)

    if id:
        # UPDATE: replace the wave's milestone list (upsert).
        wave = next((w for w in plan.waves if w.id == id), None)
        if not wave:
            ids = [w.id for w in plan.waves]
            raise ValueError(f"Wave {id} not found. Valid: {ids}")
        wave.milestones = milestones_list
        ctx.save_plan(plan)
        return {"id": wave.id, "operation": "updated"}

    if not milestones_list:
        raise ValueError("milestones required for create (empty allowed only on update)")
    wid = plan.next_wave_id()
    plan.waves.append(schema["Wave"](id=wid, milestones=milestones_list))
    ctx.save_plan(plan)
    return {"id": wid, "operation": "created"}


def _translate(ctx: PlanContext, output: str) -> dict:
    """Translate plan.json to Markdown. Internal only -- not exposed via CLI."""
    from .plan import translate_to_markdown

    plan = ctx.load_plan()
    md = translate_to_markdown(plan)
    Path(output).write_text(md, encoding="utf-8")

    return {"output": output, "operation": "created"}


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------


def validate(ctx: PlanContext, phase: str) -> dict:
    """Validate plan.json for a specific phase.

    Mirrors the CLI ValidateCommand's choices=['plan-design'] whitelist, so a
    batch payload with a non-design phase is rejected rather than silently
    returning passed (validate_completeness returns [] for non-design phases).
    """
    VALID_PHASES = frozenset({"plan-design"})
    if phase not in VALID_PHASES:
        raise ValueError(
            f"Invalid phase '{phase}' for validate. "
            f"Valid phases: {', '.join(sorted(VALID_PHASES))}"
        )
    plan = ctx.load_plan()

    errors = []
    errors.extend(plan.validate_refs())
    errors.extend(plan.validate_completeness(phase))

    if errors:
        raise ValueError("Validation errors:\n" + "\n".join(errors))

    return {"phase": phase, "status": "passed"}


# -----------------------------------------------------------------------------
# List Helpers (read-only)
# -----------------------------------------------------------------------------


def list_milestones(ctx: PlanContext) -> list[dict]:
    """List all milestones."""
    plan = ctx.load_plan()
    return [{"id": ms.id, "version": ms.version, "name": ms.name} for ms in plan.milestones]


def list_intents(ctx: PlanContext, milestone_id: str) -> list[dict]:
    """List intents in milestone."""
    plan = ctx.load_plan()

    ms = plan.get_milestone(milestone_id)
    if not ms:
        ids = [m.id for m in plan.milestones]
        raise ValueError(f"Milestone {milestone_id} not found. Valid: {ids}")

    return [
        {"id": ci.id, "version": ci.version, "file": ci.file, "behavior": ci.behavior[:50]}
        for ci in ms.code_intents
    ]


def list_decisions(ctx: PlanContext) -> list[dict]:
    """List all decisions."""
    plan = ctx.load_plan()
    return [
        {"id": dl.id, "version": dl.version, "decision": dl.decision[:50]}
        for dl in plan.planning_context.decisions
    ]
