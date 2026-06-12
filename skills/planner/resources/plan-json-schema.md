# Plan JSON Schema v2

JSON-IR-first architecture. The architect's **Code Intent is the durable contract**;
the developer implements it just-in-time against the live file at execution (there are
no plan-time diffs). `plan.json` is authoritative and is rendered to Markdown when the
plan is approved.

## Schema Overview

```
plan.json
  plan_id: uuid
  created_at: ISO-8601
  frozen_at: null | ISO-8601

  overview:
    title: string
    problem: string
    approach: string

  planning_context:
    decision_log: [DecisionLogEntry]
    rejected_alternatives: [RejectedAlternative]
    constraints: [Constraint]
    known_risks: [KnownRisk]

  invisible_knowledge:
    architecture: Diagram
    data_flow: Diagram
    structure_rationale: string
    invariants: [string]
    tradeoffs: [string]

  milestones: [Milestone]
  milestone_dependencies: MilestoneDependencies
```

---

## Decision Log Entry

Architect populates. Multi-step reasoning required.

```json
{
  "id": "DL-001",
  "decision": "What was decided",
  "reasoning_chain": "premise -> implication -> conclusion",
  "timestamp": "2024-01-15T10:30:00Z"
}
```

ID format: `DL-###` (sequential)

---

## Rejected Alternative

Link to decision that led to rejection.

```json
{
  "id": "RA-001",
  "alternative": "Use Redis for caching",
  "rejection_reason": "Team has no Redis ops experience",
  "decision_ref": "DL-001"
}
```

---

## Constraint

```json
{
  "id": "C-001",
  "type": "technical|organizational|dependency",
  "description": "Must use Python 3.10+",
  "source": "user-specified|doc-derived|inferred"
}
```

---

## Known Risk

```json
{
  "id": "R-001",
  "risk": "API rate limits may cause timeouts",
  "mitigation": "Implement exponential backoff",
  "anchor": "src/client.py:L45-L60",
  "decision_ref": "DL-002"
}
```

---

## Invisible Knowledge

Knowledge that should transfer to future LLM sessions.

```json
{
  "architecture": {
    "diagram_ascii": "Client --> Gateway --> Services",
    "description": "Request routing pattern..."
  },
  "data_flow": {
    "diagram_ascii": "Input -> Validate -> Transform -> Store",
    "description": "Data pipeline..."
  },
  "structure_rationale": "Why we organized code this way...",
  "invariants": [
    "All public APIs must validate input before processing",
    "Database connections must use connection pooling"
  ],
  "tradeoffs": [
    "Chose simplicity over performance for initial implementation",
    "Using sync IO to avoid complexity; can migrate to async later"
  ]
}
```

---

## Milestone

```json
{
  "id": "M-001",
  "number": 1,
  "name": "Implement rate limiter",
  "files": ["src/ratelimit.py", "tests/test_ratelimit.py"],
  "flags": ["error-handling", "needs-rationale"],
  "requirements": ["Limit to 100 requests per minute per client"],
  "acceptance_criteria": ["Test demonstrates rate limiting behavior"],

  "tests": {
    "files": ["tests/test_ratelimit.py"],
    "type": "unit|integration|property-based",
    "backing": "user-specified|doc-derived|default-derived",
    "scenarios": {
      "normal": ["Under limit requests succeed"],
      "edge": ["Exactly at limit"],
      "error": ["Over limit returns 429"]
    },
    "skip_reason": null
  },

  "code_intents": [...],

  "is_documentation_only": false,
  "delegated_to": null
}
```

---

## Code Intent

Architect populates — **the durable, binding contract** (you read the source; there are
no plan-time diffs). The developer implements it just-in-time against the live file at
execution, and impl-code QR reviews exactly what ships. Make it complete: per file give
symbol signatures + purpose, precise behavior (control flow, error/edge handling, data
shapes), the integration seam by name, and a `decision_ref` for every value / threshold /
tradeoff.

Fields: `id`, `file`, optional `function`, `behavior`, `decision_refs`. Encode every
threshold / value / unit inside `behavior` (prose) and cite the deciding `decision_ref`
-- there is no separate params structure.

```json
{
  "id": "CI-M-001-001",
  "file": "src/ratelimit.py",
  "function": "check_rate_limit",
  "behavior": "Return True if the request is allowed, False if rate limited. Sliding window of 60s (DL-002); count requests in the window and compare to the per-client limit.",
  "decision_refs": ["DL-001", "DL-002"]
}
```

ID format: `CI-{milestone_id}-###`

---

## Milestone Dependencies

```json
{
  "diagram_ascii": "M-001 --> M-002\n        \\--> M-003\nM-002 --> M-004\nM-003 --> M-004",
  "waves": [
    { "wave": 1, "milestones": ["M-001"] },
    { "wave": 2, "milestones": ["M-002", "M-003"] },
    { "wave": 3, "milestones": ["M-004"] }
  ]
}
```

---

## Validation Rules

### Reference Integrity

1. `code_intent.decision_refs[]` must point to existing `decision_log.id`
2. `rejected_alternative.decision_ref` must point to existing `decision_log.id`
3. `known_risk.decision_ref` must point to existing `decision_log.id`

### Phase Completeness

**plan-design** (Architect) — the only planning phase:

- `overview.problem` required
- At least one milestone
- Each non-documentation-only milestone has at least one `code_intent` (the contract)

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
