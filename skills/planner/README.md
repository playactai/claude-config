# Planner

LLM-generated plans have gaps. I have seen missing error handling, vague
acceptance criteria, specs that nobody can implement. I built this skill with
two workflows -- planning and execution -- connected by quality gates that catch
these problems early.

**Authoritative specification**: See INTENT.md for complete design rationale, invariants, and state file schemas. This README provides operational overview; INTENT.md is the source of truth for architectural decisions.

## Planning Workflow

```
  plan-init → context-verify → plan-design-work (Architect)
      |
      v
  plan-design-qr-decompose → plan-design-qr-verify
      |
      v
  plan-design-qr-route ----+
      |                    |
      v                    |
   APPROVED         [fail: restart plan-design-work]
```

| Step                    | Actions                                                                    |
| ----------------------- | -------------------------------------------------------------------------- |
| Context & Scope         | Confirm path, define scope, identify approaches, list constraints          |
| Decision & Architecture | Evaluate approaches, select with reasoning, diagram (rendered by architect), break into milestones |
| Code Intents            | Author `code_intents[]` — the binding behavioral contract per milestone    |
| QR-Design               | Verify Decision Log complete, plan structure, code intents are sufficient  |

The architect renders the dependency diagram ASCII via `cli.plan set-diagram-render`.
Code Intent is the durable contract: no plan-time unified diffs are produced.
At execution, the developer regenerates implementation just-in-time per wave against
the live file from Code Intent.

## Execution Workflow

```
  Plan --> Milestones --> impl-code QR --> exec-docs --> impl-docs QR
               ^               |
               +--- [fail] ----+

  * impl-code QR is the single authoritative code review
```

After planning completes and context clears (`/clear`), execution proceeds:

| Step                   | Purpose                                                         |
| ---------------------- | --------------------------------------------------------------- |
| Execution Planning     | Analyze plan, detect reconciliation signals, output strategy    |
| Milestone Execution    | Delegate to developers; just-in-time impl per wave from Code Intent |
| Post-Implementation QR | Authoritative code review of implemented code                   |
| Issue Resolution       | (conditional) Present issues, collect decisions, delegate fixes |
| exec-docs              | Technical writer authors ALL docs in real source (inline comments, docstrings from Decision Log + Invisible Knowledge, CLAUDE.md, README) |
| impl-docs QR           | Quality review of authored documentation                        |

The coordinator never writes code directly — it delegates to developers.
The developer adds no comments; all documentation authorship belongs to exec-docs.

- Parallelizes independent work across up to 4 developers per milestone
- Runs impl-code QR after all milestones complete
- Loops through issue resolution until QR passes
- Invokes exec-docs only after QR passes

## Invisible Knowledge

### Why session.yaml was removed

Initial design included session.yaml to track workflow state across invocations. Removed because context.json already captures task and architecture decisions -- the critical state that sub-agents need. Session-level tracking (current step, timestamps) belongs in the orchestrator's context window, not persisted state. Adding a separate file created redundancy without value.

### Why 6-field decision schema

Early design used 11 fields per decision (id, question, status, raised_at, decided_at, decided_by, answer, rationale, options, blocking, superseded_by). Reduced to 6 fields (id, question, status, decided_by, answer, rationale) because:

- raised_at/decided_at: Timestamps added noise without improving decision reasoning
- options: Better captured in findings.json during EXPLORING phase
- blocking: Implicit in status=READY with orchestrator waiting for user input
- superseded_by: Trackable via status=SUPERSEDED + new decision with same question

Simpler schema means less for LLMs to get wrong when writing decisions.

### Why per-phase qr-<phase>.json instead of single qa.json

Separate qr-<phase>.json files (qr-plan-design.json, qr-impl-code.json, qr-impl-docs.json) prevent cross-phase contamination. With a single qa.json:

- Plan QR items mix with implementation QR items (confusing for fixers)
- Verification scope unclear (which phase is this item checking?)
- Cannot isolate QR results per phase (plan QR should be independent from implementation QR)

Per-phase files allow independent verification cycles with clear boundaries. Each file is deleted when its phase passes QR gate.

## Plan Schema

Key fields in plan.json:

- milestones[].code_intents[] — binding behavioral contract (CI-XXX ids, behavior, decision_refs)
- planning_context.decision_log[] — decisions referenced by code_intents at execution
- invisible_knowledge — architecture/data-flow diagrams and rationale (sourced by exec-docs)
