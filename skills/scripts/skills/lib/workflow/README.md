# Workflow Framework

## Overview

Metadata-only workflow framework for skill registration and exhaustive testing. Skills declare their structure as a `Workflow` containing `StepDef` instances. `Workflow` is a data container — steps are introspectable and testable, but execution happens through each skill's own CLI entry point (`format_output() -> print()`), not through a central run loop.

## Architecture

```
Skills Layer (~12 modules, each with WORKFLOW = Workflow(...))
       |
       v
   Workflow API (Workflow / StepDef / Arg)
       |
       v
Discovery Layer (importlib scanning, pull-based)
       |
       v
Core Framework (metadata types, domain types, testing support)
       |
       v
CLI / Test Harness
```

### Data Flow (registration and testing)

```
CLI / pytest
      |
      v
discover_workflows("skills")    # importlib scan, no side effects
      |
      v
registry: {name -> Workflow}    # uses _module_path for CLI invocation
      |
      v
Workflow._validate()            # entry point in steps, unique step ids
      |
      v
extract_schema(workflow)        # driven by _params + _step_order
generate_inputs(workflow)       # Cartesian product of domain types
      |
      v
pytest.parametrize + subprocess runs skill CLI per (step, params)
```

### Data Flow (skill runtime)

```
CLI invocation
      |
      v
format_output(step, ...)        # skill-owned
      |
      v
print()                         # emits step body + <invoke_after>
      |
      v
LLM reads output, follows <invoke_after> to invoke next step
```

Discovery uses `importlib.import_module` + `pkgutil.walk_packages` to find `WORKFLOW` constants without executing module-level side effects. This pull-based approach eliminates import-time surprises and keeps testing isolated.

## Invocation Pattern

Three distinct invocation forms are used across the repository, chosen by caller context:

- `<invoke working-dir=".claude/skills/scripts" cmd="uv run python -m skills.X" />` — Claude Code `<invoke>` tags; `working-dir` resolves against the active `.claude/` dir.
- `uv run --project "${CLAUDE_PROJECT_DIR:-$HOME}/.claude/skills/scripts" python -m skills.X` — raw bash in code blocks; no `working-dir` resolution available.
- `uv run python -m skills.X` — Python `next_cmd` strings fed to `format_step()`; the wrapper supplies cwd via a `cd` prefix.

Full rationale and the cd-wrapper invariant are in `prompts/README.md`.

## Core Types

### Workflow

Collection of steps with validation and optional `_params` metadata for the exhaustive test generator.

```python
Workflow(
    name: str,
    *steps: StepDef,
    entry_point: str | None = None,   # defaults to first step's id
    description: str = "",
    params: dict[str, list[dict]] | None = None,  # step_id -> list of param specs
    validate: bool = True,
)
```

Introspectable attributes:

| Attribute            | Purpose                                               |
| -------------------- | ----------------------------------------------------- |
| `name`, `description`| Human-facing identifiers                              |
| `steps`              | Dict `step_id -> StepDef`                             |
| `_step_order`        | List of step ids in declaration order (CLI `--step N` indexes this) |
| `_params`            | Dict `step_id -> list of {name, required, choices, ...}` param specs |
| `_module_path`       | Set by `discover_workflows` so tests can `python -m` the skill |
| `entry_point`        | First step to run                                     |
| `total_steps`        | `len(steps)`                                          |

### StepDef

```python
@dataclass(frozen=True)
class StepDef:
    id: str
    title: str
    actions: list[str]
```

Minimal metadata. Skills attach behavior in their own `format_output()`; `StepDef` exists so introspection (docs, tests) can enumerate steps without running anything.

### Arg (Parameter Metadata)

Used by skills that declare per-step CLI parameter metadata so the exhaustive test generator can produce valid inputs.

```python
@dataclass(frozen=True)
class Arg:
    description: str = ""
    default: Any = inspect.Parameter.empty
    min: int | float | None = None
    max: int | float | None = None
    choices: tuple[str, ...] | None = None
    required: bool = False
```

Skills populate `_params` (typically by passing `params={step_id: [{"name": ..., "required": ..., "choices": ...}, ...]}` to `Workflow(...)`), and `test_generation.py` reads those specs.

### Dispatch (in `types.py`)

```python
@dataclass
class Dispatch:
    agent: AgentRole
    script: str
    context_vars: dict[str, str] = field(default_factory=dict)
    free_form: bool = False
```

Value type describing a sub-agent dispatch. Consumed by orchestrator skills that delegate work (QR review, developer, technical-writer). `AgentRole` enumerates the valid targets: QUALITY_REVIEWER, DEVELOPER, TECHNICAL_WRITER, EXPLORE, GENERAL_PURPOSE, ARCHITECT.

### ResourceProvider Protocol (in `types.py`)

```python
class ResourceProvider(Protocol):
    def get_resource(self, name: str) -> str: ...
    def get_step_guidance(self, **kwargs) -> dict: ...
```

Breaks a three-layer import coupling: QR/TW/Dev modules can accept a provider instead of importing `skills.planner.shared.resources` directly, which would create a cycle. The Protocol also enables mock implementations for isolated unit testing.

## Workflow Validation

`Workflow.__init__` enforces:

1. **Unique step ids** — no duplicates across the step tuple.
2. **Entry point exists** — `entry_point` (default: first step id) must be in `steps`.

These checks run at module load time, so malformed workflows surface as `ValueError` during `discover_workflows()` rather than during testing.

## Invariants

- Every skill module listed in `tests/conftest.py::SKILL_MODULES` exposes a `WORKFLOW = Workflow(...)` constant at module level.
- `discover_workflows()` silently skips modules with no `WORKFLOW` attribute; any import error aborts discovery (aggregated report across all failures).
- `len(workflow._step_order) == workflow.total_steps`; CLI `--step N` corresponds to index N (1-based) in `_step_order`.
- QR iteration blocking severities (defined in `skills/planner/shared/qr/constants.py`): iterations 1–2 block MUST/SHOULD/COULD; iterations 3–4 block MUST/SHOULD; iteration 5+ blocks MUST only. Progressive de-escalation prevents infinite retry loops on low-severity items.
- Handler behavior lives in the skill's own `format_output()`, not in `StepDef`. The framework is deliberately metadata-only.

## Design Decisions

**Why metadata-only (no central execution engine)?** Skills already have CLI-based step invocation (`uv run python -m skill --step N`) that works well with the LLM's Bash tool. A central `Workflow.run()` would have to re-route through the same CLI layer or duplicate it; removing it eliminates dead code and a whole class of framework-vs-skill coupling.

**Why separate `Workflow` and `StepDef`?** Workflows are collections; steps are atomic. Keeping them separate lets `Workflow.__init__` validate at the collection level (unique ids, entry point) while step definitions stay focused on per-step metadata.

**Why frozen `StepDef` and `Arg`?** They are immutable specifications. Frozen dataclasses prevent accidental mutation and make them safe to share between workflows and between threads.

**Why `_params` as a dict on the constructor instead of reading handler signatures?** Earlier drafts inspected handler signatures via `inspect` and extracted `Annotated[..., Arg(...)]` metadata. That coupled the framework to a specific handler shape. Accepting `params={step_id: [...]}` directly keeps the framework unopinionated about handlers.

**Why pull-based discovery over registration decorators?** Decorators run module-level side effects on import. Pull-based scanning (`discover_workflows`) finds `WORKFLOW` constants without requiring side effects, enabling isolated unit testing and avoiding circular import chains.

**Separate CLI entry points per skill** (as opposed to one `uv run python -m workflow run <skill>` dispatcher): Running modules as `__main__` causes module identity issues (the module is imported via its qualified name by `__init__.py` but would be re-executed as `__main__` under a central dispatcher). Separate CLI entry points avoid this duplicate-import trap.

## Tradeoffs

**Idiomatic API vs minimal**: Higher refactoring cost for cross-skill consistency. The deepthink pattern proved that `Workflow`/`StepDef` works; extending it to every skill was worth the churn for the uniformity gain.

**Centralized enums vs local**: One more place to update when adding a state, in exchange for shared vocabulary (`AgentRole`, `LoopState`, `Confidence`) that makes state machines explicit and enables property-based testing across skills.

**Clean break vs dual-path**: We chose a clean break when removing the execution engine. No callers outside the repo, so the transition cost was bounded; dual-path would have added maintenance burden with no benefit.

## Exhaustive Testing Framework

`tests/test_workflow_steps.py` parametrizes every (step, param-combination) for every registered workflow and runs each via subprocess. Domain types in `types.py` drive the Cartesian product.

### Data Flow

```
Workflow objects            Domain types               Generation
     |                           |                          |
     v                           v                          v
workflow._params       BoundedInt / ChoiceSet      extract_schema(workflow)
workflow._step_order   Constant                    generate_inputs(workflow)
                                                         |
                                                         v
                                         pytest.parametrize + subprocess
```

### Domain Types (in `types.py`)

All three are frozen dataclasses implementing `__iter__` for use with `itertools.product`. `frozen=True` enables hashability for pytest param caching.

| Type         | Shape                | Example                                           |
| ------------ | -------------------- | ------------------------------------------------- |
| `BoundedInt` | inclusive `[lo, hi]` | `list(BoundedInt(1, 3))` -> `[1, 2, 3]`           |
| `ChoiceSet`  | tuple of choices     | `list(ChoiceSet(("full", "quick")))` -> `["full", "quick"]` |
| `Constant`   | single fixed value   | `list(Constant(42))` -> `[42]`                    |

### Why This Structure

Domain types separate from generation logic:

- **Reusable** — the same domain types could drive fuzzing or documentation, not just pytest.
- **Decoupled** — generation logic depends on workflow structure (step order, params), not domain semantics.
- **Single test file** adds pytest-specific concerns (param ids, subprocess runner) without mixing them into the core framework.

### Key Design Decisions

**Exhaustive vs sampling**: Domains are small (~300–500 total combinations across all workflows). Exhaustive enumeration catches edge combinations that sampling would miss. Sampling would save seconds and risk missed regressions.

**Hardcoded mode-gating**: Only deepthink has a mode parameter (quick mode skips steps 6–11). Introspection machinery for the general case is not justified for a single consumer; `test_generation.py::get_mode_gated_steps` hardcodes the deepthink rule explicitly.

**`_params` keyed by step_id (string), not step number**: `_step_order` supplies the authoritative index mapping for CLI invocation. Keying param specs by string id keeps them stable if steps are reordered.

## Question Relay Protocol

Sub-agents can request user clarification via the main agent. The protocol is pure prompt coordination — no Python interception.

### Design Decisions

**Reinvocation over resume**: When a sub-agent yields with questions, the orchestrator REINVOKES it fresh (new Task, no resume parameter) after getting user answers. The sub-agent saves state to `plan.json` before yielding and reads it back after reinvocation. This was chosen over resume because:

- Resume semantics are unreliable (0-token / 0-tool-use failures observed in the field).
- State file reading is explicit and auditable.
- Clean slate avoids stale context issues.
- Sub-agent scripts can detect continuation by checking whether `plan.json` exists.

**Questions-only output**: When a sub-agent needs clarification, it emits ONLY the `<needs_user_input>` XML block — nothing else. This makes detection unambiguous (no heuristic parsing of natural language).

**Explicit XML markers**: Structured tags rather than question-mark detection prevent false positives from rhetorical questions in analysis output.

**Max 3 questions, 2–3 options each**: Matches the `AskUserQuestion` tool schema. Batching reduces round-trips. Options must be distinct and actionable.

**State saving before yield**: Sub-agents MUST persist all progress before emitting `<needs_user_input>`. The reinvoked instance has no in-memory continuity and reads `plan.json` fresh.

### Flow

1. Sub-agent saves current state to `plan.json`.
2. Sub-agent emits `<needs_user_input>` XML as its entire response.
3. Main agent extracts questions, calls `AskUserQuestion`.
4. Main agent reinvokes the sub-agent fresh with answers and `STATE_DIR`.
5. New sub-agent instance reads `plan.json` and continues from saved state.

### Relevant Constants (`constants.py`)

| Constant                    | Purpose                                      |
| --------------------------- | -------------------------------------------- |
| `SUB_AGENT_QUESTION_FORMAT` | Tells sub-agent how to emit questions        |
| `QUESTION_RELAY_HANDLER`    | Tells main agent how to detect and relay     |

### Integration

For dispatch steps that support question relay:

```python
from skills.lib.workflow.constants import QUESTION_RELAY_HANDLER

if step_info.get("supports_questions"):
    actions.append(QUESTION_RELAY_HANDLER)
```

For sub-agent scripts that may ask questions, include `SUB_AGENT_QUESTION_FORMAT` in step 1 guidance.

## Testing

All tests use pytest. Run via uv. Set `SCRIPTS="${CLAUDE_PROJECT_DIR:-$HOME}/.claude/skills/scripts"` so the commands work against both the user-global install and a project-local `.claude/`:

```bash
SCRIPTS="${CLAUDE_PROJECT_DIR:-$HOME}/.claude/skills/scripts"
uv run --project "$SCRIPTS" pytest "$SCRIPTS" -v                                          # everything
uv run --project "$SCRIPTS" pytest "$SCRIPTS" -k deepthink -v                             # one workflow
uv run --project "$SCRIPTS" pytest "$SCRIPTS/tests/test_workflow_import.py" -v            # imports only
uv run --project "$SCRIPTS" pytest "$SCRIPTS/tests/test_workflow_structure.py" -v         # structure validation
uv run --project "$SCRIPTS" pytest "$SCRIPTS/tests/test_workflow_steps.py" -v             # exhaustive step invocability
uv run --project "$SCRIPTS" pytest "$SCRIPTS/tests/test_domain_types.py" -v               # domain type units
```
