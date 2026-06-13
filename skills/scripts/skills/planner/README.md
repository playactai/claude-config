# Planner

Planning and execution workflows with QR (Quality Review) gates, TW (Technical Writer) passes, and Dev (Developer) execution phases.

This document is authoritative for the planner skill architecture.

## Architecture: Python Scripts vs LLM

Python scripts emit workflow prompts and routing. The LLM operates BETWEEN script invocations:

1. Script outputs prompt/guidance for current step
2. LLM reads prompt, performs reasoning/assessment
3. LLM decides outcome (e.g., QR PASS/FAIL)
4. LLM invokes next script based on outcome

QR PASS/FAIL is determined by LLM reading QR output, not Python. Gate routing is LLM's decision based on QR outcome. Python scripts provide structure; LLM provides intelligence.

## State Files

All state mutations (except initial context.json) happen via Python CLI commands. State directory created via `tempfile.mkdtemp()` in `/tmp`.

| File              | Schema         | Created     | Mutated By     | Lifecycle              |
| ----------------- | -------------- | ----------- | -------------- | ---------------------- |
| `plan.json`       | Pydantic v2    | Step 1 init | CLI commands   | mutable                |
| `context.json`    | Loose JSON     | Step 2      | LLM Write tool | frozen after step 2    |
| `qr-{phase}.json` | QA item schema | QR dispatch | LLM during QR  | ephemeral per QR cycle |

### plan.json Schema

```
Plan
  plan_id: UUID                      (auto)
  created_at: ISO-8601 timestamp     (auto)

  overview:
    problem, approach

  planning_context:
    decisions[]: id (DL-XXX), version, decision, reasoning   (input alias: decision_log; reasoning <- reasoning_chain)
    rejected_alternatives[]: id (RA-XXX), alternative, rejection_reason, decision_ref
    constraints[]: plain strings (no IDs/types)
    risks[]: id (R-XXX), risk, mitigation, anchor?, decision_ref?   (input alias: known_risks)

  invisible_knowledge:
    system, invariants[], tradeoffs[]   (diagrams live in diagram_graphs, not here)

  milestones[]:
    id (M-XXX), version, number, name, files[], flags[], requirements[], acceptance_criteria[]
    tests[]: flat list of free-form descriptions
    code_intents[]: id (CI-XXX), version, file, function?, behavior, decision_refs[]
    is_documentation_only, delegated_to?

  waves[]: id (W-XXX), milestones[]   (top-level; no separate milestone_dependencies block)
  diagram_graphs[]: id (DIAG-XXX), type, scope, title, nodes[], edges[], ascii_render?
```

Reference integrity: code_intent.decision_refs -> decisions[].id.
Authoritative schema: `resources/plan-json-schema.md` (mirrors `shared/schema.py`).
No `schema_version` field -- state files are ephemeral (one planning session).

### context.json Schema

User-provided context captured during planning:

```json
{
  "task_spec": ["goal", "scope", "out-of-scope"],
  "constraints": ["MUST: X", "SHOULD: Y"],
  "entry_points": ["file:function - why"],
  "rejected_alternatives": ["alternative - why dismissed"],
  "current_understanding": ["how system works"],
  "assumptions": ["inference (confidence)"],
  "invisible_knowledge": ["design rationale", "invariants"],
  "user_quotes": ["verbatim quote"]
}
```

### qr-{phase}.json Schema

Phases: `qr-plan-design`, `qr-impl-code`, `qr-impl-docs`

```json
{
  "phase": "plan-design",
  "iteration": 1,
  "items": [
    {
      "id": "qa-001",
      "scope": "*",
      "check": "...",
      "status": "TODO|PASS|FAIL",
      "finding": null
    }
  ]
}
```

## Workflow Phases and Mutations

### Planner Workflow (6 steps)

| Step | Name                    | Pattern Function          | Mutates              | Agent        |
| ---- | ----------------------- | ------------------------- | -------------------- | ------------ |
| 1    | plan-init               | `init_step()`             | Creates plan.json    | Orchestrator |
| 2    | context-verify          | `verify_step()`           | Creates context.json | Orchestrator |
| 3    | plan-design-work        | `execute_dispatch_step()` | plan.json            | Architect    |
| 4    | plan-design-qr-decompose| `qr_dispatch_step()`      | qr-plan-design.json  | QR           |
| 5    | plan-design-qr-verify   | `qr_dispatch_step()`      | qr-plan-design.json  | QR           |
| 6    | plan-design-qr-route    | `qr_gate_step()`          | Renders plan.md (PASS) | Orchestrator |

Terminal on PASS at step 6: **PLAN APPROVED**.

**Mutation details**:

- Step 3 (Architect): Populates planning_context, milestones[], code_intents[], invisible_knowledge, renders diagram ASCII via `cli.plan set-diagram-render`

Code Intent (`code_intents[]`) is the binding behavioral contract. There are no plan-time unified diffs. At execution the developer regenerates implementation just-in-time per wave against the live file from Code Intent.

### Executor Workflow (10 steps)

| Step | Name              | Mutates           | Agent        |
| ---- | ----------------- | ----------------- | ------------ |
| 1    | init              | -                 | Orchestrator |
| 2    | load-verify       | -                 | Orchestrator |
| 3    | impl-execute      | Codebase files    | Developer    |
| 4    | impl-code-qr-decompose | qr-impl-code.json | QR      |
| 5    | impl-code-qr-verify   | qr-impl-code.json | QR      |
| 6    | impl-code-qr-gate | -                 | Orchestrator |
| 7    | impl-docs-execute | Codebase docs     | TW           |
| 8    | impl-docs-qr-decompose | qr-impl-docs.json | QR     |
| 9    | impl-docs-qr-verify   | qr-impl-docs.json | QR     |
| 10   | impl-docs-qr-gate | -                 | Orchestrator |

impl-code QR is the single authoritative code review. exec-docs (impl-docs phase) authors ALL documentation directly in the real implemented source (inline comments, docstrings from Decision Log + Invisible Knowledge, plus CLAUDE.md and README).

## Components

```
orchestrator/
  planner.py      6-step planning workflow
  executor.py     10-step execution workflow

architect/
  plan_design.py  Plan creation (exploration, milestones, code_intents, diagram render)

developer/
  exec_implement.py  Wave-aware implementation (just-in-time from code_intents)

technical_writer/
  exec_docs.py    Post-implementation docs (inline comments, docstrings, CLAUDE.md, README)

quality_reviewer/
  qr_decompose.py              QR decompose runner (--phase plan-design|impl-code|impl-docs)
  qr_verify.py                 QR verify runner (--phase ...)
  qr_verify_base.py            VerifyBase ABC + verify_main
  prompts/content.py           Per-phase decompose prompts + verifier classes
  prompts/decompose.py         Shared 13-step decompose flow (dispatch_step)

shared/
  resources.py    Path derivation, context loading
  builders.py     XML output builders
  constraints.py  Orchestrator constraint AST builders
  qr/             QR utilities (types, constants, utils, schema)

state/
  models.py       Pydantic v2 schemas for plan.json
  validator.py    Validation functions
  decisions.py    Decision lifecycle enum (reserved for future use)

cli/
  plan.py         plan.json manipulation commands
```

## QR Gate Mechanics

QR gates use LoopState enum: INITIAL -> RETRY -> COMPLETE

```
INITIAL -> PASS -> COMPLETE (terminal)
INITIAL -> FAIL -> RETRY (iteration++)
RETRY   -> FAIL -> RETRY (iteration++)
RETRY   -> PASS -> COMPLETE (terminal)
```

Blocking severity by iteration:

| Iteration | Blocks              |
| --------- | ------------------- |
| 1-2       | MUST, SHOULD, COULD |
| 3         | MUST, SHOULD        |
| 4+        | MUST only           |

## Step Handler Architecture

Closures capture static config, handlers receive dynamic state:

```python
def execute_dispatch_step(title, agent, script, ...):
    def handler(ctx):  # Receives state_dir, qr, qr_fail
        return {"title": ..., "actions": ..., "next": ...}
    return handler

STEPS = {
    1: init_step("plan-init", ...),
    3: execute_dispatch_step("plan-design-execute", agent="architect", ...),
    4: qr_dispatch_step("plan-design-qr", ...),
    5: qr_gate_step("plan-design-qr-gate", ...),
}
```

## Design Decisions

**Closure-based step dispatch**: STEPS dict maps step numbers to handler closures. Pattern functions capture static config (title, agent, script), handlers receive dynamic state via ctx. Replaces magic keys with explicit patterns.

**Convention-based paths**: Sub-agents receive --state-dir, derive file paths via get_context_path(). Changing context.json location requires only updating resources.py.

**LLM-managed state**: State files written by LLM agents reading step guidance, not Python scripts. Leverages LLM capabilities for understanding context and following formats.

**JSON-IR-First**: plan.json is authoritative; plan.md derived from it.

**QR iteration blocking**: Severity thresholds vary by iteration. Early iterations block all severities. Later iterations block only MUST to prevent infinite loops.

**No temp directory cleanup**: OS handles /tmp cleanup on reboot.

## Invariants

1. Every skill entry point defines exactly ONE Workflow
2. discover_workflows() finds all Workflows without import errors
3. plan.json is self-contained for execution
4. qr-{phase}.json files are ephemeral (exist only during QR cycle)
5. QR iteration blocking: iter 1-2 all; iter 3 MUST/SHOULD; iter 4+ MUST only
