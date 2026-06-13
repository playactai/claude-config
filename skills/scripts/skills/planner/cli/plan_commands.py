"""Plan manipulation commands as plain functions.

Each public function with 'ctx' as first param is auto-discovered as RPC method.
Function names use underscores, converted to hyphens for method names.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from ..shared.schema import Plan


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
    """Context passed to all plan commands."""

    state_dir: Path

    def plan_path(self) -> Path:
        return self.state_dir / "plan.json"

    def state_file(self) -> Path:
        """Single mutable state file (used by batch snapshot/rollback)."""
        return self.plan_path()

    def load_plan(self) -> Plan:
        schema = _get_schema()
        path = self.plan_path()
        if not path.exists():
            raise FileNotFoundError(f"plan.json not found at {path}")
        data = json.loads(path.read_text())
        return schema["Plan"].model_validate(data)

    def save_plan(self, plan: Plan) -> None:
        """Validate the in-memory plan, then atomically persist it.

        Mirrors the module-level cli.plan.save_plan: validate cross-references
        BEFORE writing, so a rejected mutation never reaches disk (no bad write,
        no rollback) and an unrelated malformed qr-{phase}.json does not fail a
        valid plan mutation. The batch path layers its own transaction snapshot on
        top for all-or-nothing across multiple commands.
        """
        from skills.lib.io import atomic_write_text

        from ..shared.schema import SchemaValidationError

        errors = plan.validate_refs()
        if errors:
            raise SchemaValidationError(f"plan.json: {errors}")
        atomic_write_text(self.plan_path(), plan.model_dump_json(indent=2))


def _parse_csv(value: str | None) -> list[str]:
    """Parse comma-separated string to list."""
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _validate_relpath(path: str, context: str) -> str:
    """Reject absolute and parent-relative paths so the overlap guard works.

    validate_refs compares lexical os.path.normpath strings; abs-vs-rel
    and differently-rooted relative spellings of the same file slip past
    and let the executor co-schedule two milestones that race-write one
    file. Enforcing the relative-path convention at write time closes
    that gap without touching the filesystem at plan time (realpath).
    """
    if not path:
        return path
    stripped = path.strip()
    if os.path.isabs(stripped):
        raise ValueError(f"Absolute path not allowed in {context}: {stripped}")
    if stripped.startswith(".."):
        raise ValueError(f"Parent-relative path not allowed in {context}: {stripped}")
    return stripped


def _check_version(entity, provided: int | None, entity_id: str) -> None:
    """CAS version check. Raises if mismatch."""
    if provided is None:
        return
    current = getattr(entity, "version", 1)
    if provided != current:
        raise ValueError(
            f"Version mismatch for {entity_id}: provided {provided}, current {current}. "
            f"Re-read entity and retry with --version {current}"
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

    files_list = _parse_csv(files)
    flags_list = _parse_csv(flags)
    requirements_list = _parse_csv(requirements)
    acceptance_list = _parse_csv(acceptance_criteria)
    tests_list = _parse_csv(tests)

    if files_list:
        for f in files_list:
            _validate_relpath(f, "milestone --files")

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
        # Reversible: None = leave as-is; True/False set explicitly. Marking doc-only
        # means "no code" (doc-only <=> no code_intents); clear any existing intents here
        # so the save can't pass while final validate_completeness rejects the plan --
        # there is no delete-intent op to recover it otherwise.
        cleared_intents = 0
        dropped_from_waves = 0
        if documentation_only is not None:
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

        _bump_version(ms)
        ctx.save_plan(plan)
        result = {"id": ms.id, "version": ms.version, "operation": "updated"}
        if cleared_intents:
            result["cleared_code_intents"] = cleared_intents
        if dropped_from_waves:
            result["dropped_from_waves"] = dropped_from_waves
        return result
    else:
        # CREATE
        if version is not None:
            raise ValueError("--version only valid for updates (when --id provided)")
        if not name:
            raise ValueError("--name required for create")

        num = len(plan.milestones) + 1
        mid = f"M-{num:03d}"

        ms = schema["Milestone"](
            id=mid,
            version=1,
            number=num,
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
            f"milestone {milestone} is documentation-only; clear it with "
            f"set-milestone --no-documentation-only before adding code intents"
        )

    if file:
        _validate_relpath(file.strip(), "set-intent --file")

    refs_list = _parse_csv(decision_refs)
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
            raise ValueError("--version only valid for updates")
        if not file or not behavior:
            raise ValueError("--file and --behavior required for create")

        num = len(ms.code_intents) + 1
        cid = f"CI-{ms.id}-{num:03d}"

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
            raise ValueError("--version only valid for updates")
        if not decision or not reasoning:
            raise ValueError("--decision and --reasoning required for create")

        num = len(plan.planning_context.decisions) + 1
        did = f"DL-{num:03d}"

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
        next_num = len(plan.diagram_graphs) + 1
        new_id = f"DIAG-{next_num:03d}"
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

    dg.ascii_render = content_path.read_text()
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

    milestones_list = _parse_csv(milestones)

    if id:
        # UPDATE: replace the wave's milestone list (upsert).
        wave = next((w for w in plan.waves if w.id == id), None)
        if not wave:
            ids = [w.id for w in plan.waves]
            raise ValueError(f"Wave {id} not found. Valid: {ids}")
        wave.milestones = milestones_list
        ctx.save_plan(plan)
        return {"id": wave.id, "operation": "updated"}

    wid = plan.next_wave_id()
    plan.waves.append(schema["Wave"](id=wid, milestones=milestones_list))
    ctx.save_plan(plan)
    return {"id": wid, "operation": "created"}


def _translate(ctx: PlanContext, output: str) -> dict:
    """Translate plan.json to Markdown. Internal only -- not exposed via CLI."""
    from .plan import translate_to_markdown

    plan = ctx.load_plan()
    md = translate_to_markdown(plan)
    Path(output).write_text(md)

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
