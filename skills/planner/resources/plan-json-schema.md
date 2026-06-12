# Plan JSON Schema

JSON-IR-first architecture. The architect's **Code Intent is the durable contract**;
the developer implements it just-in-time against the live file at execution (there are
no plan-time diffs). `plan.json` is authoritative and is rendered to Markdown when the
plan is approved.

This reference mirrors the Pydantic models in `skills/planner/shared/schema.py` (the sole
source of truth). Build `plan.json` with the CLI (`set-decision`, `set-milestone`,
`set-intent`, `set-diagram*`) — do not hand-write it. State files are ephemeral (one
planning session); there is no `schema_version` field.

## Schema Overview

```
plan.json
  plan_id: uuid                      (auto)
  created_at: ISO-8601               (auto)
  frozen_at: null | ISO-8601

  overview:
    problem: string
    approach: string

  planning_context:
    decisions: [Decision]
    rejected_alternatives: [RejectedAlternative]
    constraints: [string]
    risks: [Risk]

  invisible_knowledge:
    system: string
    invariants: [string]
    tradeoffs: [string]

  milestones: [Milestone]
  waves: [Wave]
  diagram_graphs: [DiagramGraph]
```

Input aliases (accepted, but the canonical names above are what is stored):
`planning_context.decisions` ← `decision_log`, `planning_context.risks` ← `known_risks`,
`Decision.reasoning` ← `reasoning_chain`.

---

## Decision

Architect populates. Multi-step reasoning required. CLI: `set-decision`.

```json
{
  "id": "DL-001",
  "version": 1,
  "decision": "What was decided",
  "reasoning": "premise -> implication -> conclusion"
}
```

ID format: `DL-###` (sequential). `version` drives CAS optimistic locking on update.

---

## Rejected Alternative

Link to the decision that led to rejection.

```json
{
  "id": "RA-001",
  "alternative": "Use Redis for caching",
  "rejection_reason": "Team has no Redis ops experience",
  "decision_ref": "DL-001"
}
```

---

## Constraints

A plain list of strings on `planning_context.constraints` (no IDs, no types):

```json
["MUST use Python 3.10+", "MUST NOT add new runtime dependencies"]
```

---

## Risk

```json
{
  "id": "R-001",
  "risk": "API rate limits may cause timeouts",
  "mitigation": "Implement exponential backoff",
  "anchor": "src/client.py:L45-L60",
  "decision_ref": "DL-002"
}
```

`anchor` and `decision_ref` are optional (`null` when absent).

---

## Invisible Knowledge

Knowledge that should transfer to future LLM sessions. Three fields only — architecture
and data-flow diagrams live in `diagram_graphs` (scope `invisible_knowledge`), not here.

```json
{
  "system": "One-paragraph orientation to the system being changed.",
  "invariants": [
    "All public APIs validate input before processing",
    "Database connections use connection pooling"
  ],
  "tradeoffs": [
    "Chose simplicity over performance for the initial implementation",
    "Sync IO to avoid complexity; can migrate to async later"
  ]
}
```

---

## Milestone

CLI: `set-milestone`. `tests` is a flat list of free-form descriptions.

```json
{
  "id": "M-001",
  "version": 1,
  "number": 1,
  "name": "Implement rate limiter",
  "files": ["src/ratelimit.py", "tests/test_ratelimit.py"],
  "flags": ["error-handling", "needs-rationale"],
  "requirements": ["Limit to 100 requests per minute per client"],
  "acceptance_criteria": ["Test demonstrates rate limiting behavior"],
  "tests": ["unit: under-limit succeeds; at-limit edge; over-limit returns 429"],
  "code_intents": [ "...see Code Intent..." ],
  "is_documentation_only": false,
  "delegated_to": null
}
```

A **documentation-only** milestone sets `is_documentation_only: true` and carries NO
`code_intents` (the two are mutually exclusive — see Validation Rules). exec-docs authors
its `files` to satisfy its `acceptance_criteria` at execution; impl-docs QR verifies them.

---

## Code Intent

Architect populates — **the durable, binding contract** (you read the source; there are
no plan-time diffs). The developer implements it just-in-time against the live file at
execution, and impl-code QR reviews exactly what ships. Make it complete: per file give
symbol signatures + purpose, precise behavior (control flow, error/edge handling, data
shapes), the integration seam by name, and a `decision_ref` for every value / threshold /
tradeoff.

CLI: `set-intent`. Encode every threshold / value / unit inside `behavior` (prose) and
cite the deciding `decision_ref` — there is no separate params structure.

```json
{
  "id": "CI-M-001-001",
  "version": 1,
  "file": "src/ratelimit.py",
  "function": "check_rate_limit",
  "behavior": "Return True if the request is allowed, False if rate limited. Sliding window of 60s (DL-002); count requests in the window and compare to the per-client limit.",
  "decision_refs": ["DL-001", "DL-002"]
}
```

`function` is optional (`null`). ID format: `CI-{milestone_id}-###`.

---

## Wave

Top-level `waves`: each wave groups milestone IDs that execute in parallel; waves run in
order. Mirrors the `Wave` model (`id`, `milestones`) — there is no separate
`milestone_dependencies` block. CLI: `set-wave --milestones M-001,M-002` (architect).
Do not co-schedule two milestones that touch the same file in one wave — they run as
concurrent developer agents and would corrupt it mid-write.

```json
[
  { "id": "W-001", "milestones": ["M-001"] },
  { "id": "W-002", "milestones": ["M-002", "M-003"] },
  { "id": "W-003", "milestones": ["M-004"] }
]
```

---

## Diagram Graph

Architecture / state / sequence / dataflow diagrams as graph IR with an optional ASCII
render. CLI: `set-diagram`, `add-diagram-node`, `add-diagram-edge`, `set-diagram-render`.

```json
{
  "id": "DIAG-001",
  "type": "architecture",
  "scope": "overview",
  "title": "System Overview",
  "nodes": [{ "id": "client", "label": "Client", "type": "service" }],
  "edges": [
    { "source": "client", "target": "server", "label": "sends request", "protocol": "gRPC" }
  ],
  "ascii_render": "+--------+      +--------+\n| Client | ---> | Server |\n+--------+      +--------+"
}
```

`type` ∈ {architecture, state, sequence, dataflow}. `scope` is `overview`,
`invisible_knowledge`, or `milestone:M-XXX`. `node.type`, `edge.protocol`, and
`ascii_render` are optional.

---

## Validation Rules

### Reference Integrity

1. `code_intent.decision_refs[]` must point to an existing `decisions[].id`
2. `rejected_alternative.decision_ref` must point to an existing `decisions[].id`
3. `risk.decision_ref` (when present) must point to an existing `decisions[].id`
4. Every diagram `edge.source` / `edge.target` must reference a node in that diagram
5. A diagram `scope` of `milestone:M-XXX` must reference an existing milestone
6. Every `waves[].milestones[]` ID must reference an existing milestone, and no two
   milestones in the same wave may share a `files[]` entry (concurrent-write guard)

### Phase Completeness

**plan-design** (Architect) — the only planning phase:

- `overview.problem` required
- At least one milestone
- Each non-documentation-only milestone has at least one `code_intent` (the contract)
- A documentation-only milestone has NO `code_intents` (mutually exclusive)
- Every non-documentation-only milestone appears in exactly one wave
- Documentation-only milestones appear in NO wave (they route to exec-docs)

Execution runs from this plan (developer implements Code Intent JIT, then code/docs QR);
there is no plan-code or plan-docs phase.

---

## Temporal Contamination

All string fields must avoid:

1. **Change-relative**: "will be added", "new function", "modified to"
2. **Baseline reference**: "original", "existing", "current"
3. **Location directive**: "see below", "above section"
4. **Planning artifact**: "TODO", "FIXME", "implement later"
5. **Intent leakage**: "should", "needs to", "must be implemented"

Write as if code already exists in final state.
