# Default Conventions

These conventions apply when project documentation does not specify otherwise.

## Priority Hierarchy

Higher tiers override lower. Cite backing source when auditing.

| Tier | Source          | Action                           |
| ---- | --------------- | -------------------------------- |
| 1    | user-specified  | Explicit user instruction: apply |
| 2    | doc-derived     | CLAUDE.md / project docs: apply  |
| 3    | default-derived | This document: apply             |
| 4    | assumption      | No backing: CONFIRM WITH USER    |

## Severity Levels

See `severity.md` for full definitions.

| Level  | Meaning                  |
| ------ | ------------------------ |
| MUST   | Unrecoverable if missed  |
| SHOULD | Maintainability debt     |
| COULD  | Auto-fixable, low impact |

---

## Structural Conventions

<default-conventions domain="god-object">
**God Object**: >15 public methods OR >10 dependencies OR mixed concerns (networking + UI + data)
Severity: SHOULD
</default-conventions>

<default-conventions domain="god-function">
**God Function**: >50 lines OR multiple abstraction levels OR >3 nesting levels
Severity: SHOULD
Exception: Inherently sequential algorithms or state machines
</default-conventions>

<default-conventions domain="duplicate-logic">
**Duplicate Logic**: Copy-pasted blocks, repeated error handling, parallel near-identical functions
Severity: SHOULD
</default-conventions>

<default-conventions domain="dead-code">
**Dead Code**: No callers, impossible branches, unread variables, unused imports
Severity: COULD
</default-conventions>

<default-conventions domain="inconsistent-error-handling">
**Inconsistent Error Handling**: Mixed exceptions/error codes, inconsistent types, swallowed errors
Severity: SHOULD
Exception: Project specifies different handling per error category
</default-conventions>

---

## Structural Simplification Conventions

These push the structural checks toward ambition: prefer deleting complexity over
rearranging it. Flag only when a concrete, behavior-preserving restructuring is
visible -- name the simpler structure and what it removes. A vague "could be
cleaner" is not a finding.

<default-conventions domain="missed-simplification">
**Missed Simplification**: A behavior-preserving restructuring is available that would delete whole branches, helpers, modes, or layers ("code judo"), but the change rearranges or preserves the complexity instead.
Severity: SHOULD
Test: Can you name the simpler structure and the concepts it removes? If not, do not flag.
</default-conventions>

<default-conventions domain="file-size-explosion">
**File-Size Explosion**: A diff grows a single file from under 1000 lines to over 1000 lines.
Severity: SHOULD
Treatment: Presumptive decomposition trigger, not an automatic defect. Ask whether the new code should be extracted into focused modules/helpers first.
Exception: Compelling structural reason AND the file stays clearly organized (e.g., generated code, one cohesive lookup table).
</default-conventions>

<default-conventions domain="spaghetti-conditional">
**Spaghetti Conditional Growth**: New ad-hoc special-case branches, one-off booleans, or nullable modes threaded into an existing or shared flow, increasing tangle.
Severity: SHOULD
Remedy: Push the logic behind a dedicated abstraction, dispatcher, or explicit state model instead of an unrelated path.
Exception: One documented special case at a natural boundary.
</default-conventions>

<default-conventions domain="thin-abstraction">
**Thin Abstraction**: Identity wrappers, pass-through helpers, or generic "magic" mechanisms that add indirection without buying clarity, often hiding a simple data-shape assumption.
Severity: SHOULD
Remedy: Delete the wrapper and keep the direct flow, or replace the generic mechanism with the explicit structure it obscures.
Exception: Wrapper enforces a real boundary (stable public API, test seam, adapter over a volatile dependency).
</default-conventions>

<default-conventions domain="boundary-type-erosion">
**Boundary Type Erosion**: Unnecessary cast, `any`/`unknown`, or optionality that papers over an invariant a clearer type boundary should make explicit.
Severity: SHOULD
Remedy: Make the boundary explicit (typed model, narrowed union, parse-don't-validate) so downstream branches and fallbacks disappear.
Exception: Genuinely dynamic boundary (deserialization edge, untyped third-party surface) with validation at the seam.
</default-conventions>

<default-conventions domain="canonical-duplication">
**Canonical Duplication**: A bespoke helper reimplements something the codebase already provides as a canonical utility.
Severity: SHOULD
Remedy: Reuse the canonical helper; extend it if the new case is legitimate.
Exception: The canonical contract genuinely does not fit and extending it would couple unrelated concerns.
</default-conventions>

<default-conventions domain="layer-leak">
**Layer Leak**: Feature-specific logic placed in a shared/general-purpose path, an implementation detail leaking through an API, or logic living in the wrong package/layer.
Severity: SHOULD
Remedy: Move the logic to the package/module/layer that already owns the concept; keep shared paths feature-agnostic.
Exception: Project documents the placement, or a deliberate shared-kernel boundary.
</default-conventions>

<default-conventions domain="non-atomic-orchestration">
**Non-Atomic Orchestration**: Independent work serialized for no reason, or related updates structured so a failure can leave half-applied state, where an atomic or parallel structure is obvious.
Severity: SHOULD
Remedy: Run independent steps in parallel, or group related updates so they apply (and roll back) together.
Exception: Ordering is a real dependency, or atomicity is provided at another layer (transaction, idempotent retry).
</default-conventions>

---

## Conformance Conventions

<default-conventions domain="convention-violation">
**Convention Violation**: Code or plan violates a convention documented in project docs (CLAUDE.md, README.md, CONTRIBUTING). Project documentation is the specification -- cite the exact standard when flagging; never report a personal style preference as a convention violation.
Severity: SHOULD
</default-conventions>

<default-conventions domain="testing-strategy-violation">
**Testing Strategy Violation**: Tests contradict the confirmed or default test strategy. See "Testing Conventions" and "Testing Strategy Defaults" below for the strategy detail.
Severity: SHOULD
</default-conventions>

---

## File Organization Conventions

<default-conventions domain="test-organization">
**Test Organization**: Extend existing test files; create new only when:
- Distinct module boundary OR >500 lines OR different fixtures required
Severity: SHOULD (for unnecessary fragmentation)
</default-conventions>

<default-conventions domain="file-creation">
**File Creation**: Prefer extending existing files; create new only when:
- Clear module boundary OR >300-500 lines OR distinct responsibility
Severity: COULD
</default-conventions>

---

## Testing Conventions

<default-conventions domain="testing">
**Principle**: Test behavior, not implementation. Fast feedback.

**Test Type Hierarchy** (preference order):

1. **Integration tests** (highest value)
   - Test end-user verifiable behavior
   - Use real systems/dependencies (e.g., testcontainers)
   - Verify component interaction at boundaries
   - This is where the real value lies

2. **Property-based / generative tests** (preferred)
   - Cover wide input space with invariant assertions
   - Catch edge cases humans miss
   - Use for functions with clear input/output contracts

3. **Unit tests** (use sparingly)
   - Only for highly complex or critical logic
   - Risk: maintenance liability, brittleness to refactoring
   - Prefer integration tests that cover same behavior

**Test Placement**: Tests are part of implementation milestones, not separate
milestones. A milestone is not complete until its tests pass. This creates fast
feedback during development.

**DO**:

- Integration tests with real dependencies (testcontainers, etc.)
- Property-based tests for invariant-rich functions
- Parameterized fixtures over duplicate test bodies
- Test behavior observable by end users

**DON'T**:

- Test external library/dependency behavior (out of scope)
- Unit test simple code (maintenance liability exceeds value)
- Mock owned dependencies (use real implementations)
- Test implementation details that may change
- One-test-per-variant when parametrization applies

Severity: SHOULD (violations), COULD (missed opportunities)
</default-conventions>

---

## Modernization Conventions

<default-conventions domain="version-constraints">
**Version Constraint Violation**: Features unavailable in project's documented target version
Requires: Documented target version
Severity: SHOULD
</default-conventions>

<default-conventions domain="modernization">
**Modernization Opportunity**: Legacy APIs, verbose patterns, manual stdlib reimplementations
Severity: COULD
Exception: Project requires legacy pattern
</default-conventions>

---

## Testing Strategy Defaults

<default-conventions domain="testing-strategy">
**Default Test Type Preferences** (apply when project docs silent):

| Type        | Default Strategy            | Rationale                 |
| ----------- | --------------------------- | ------------------------- |
| Unit        | Property-based (quickcheck) | Few tests, many variables |
| Integration | Behavior-focused, real deps | End-user verifiable       |
| E2E         | Generated datasets          | Deterministic replay      |

These are Tier 3 defaults. User confirmation (Tier 1) overrides.

Severity: TESTING_STRATEGY_VIOLATION (SHOULD) if contradicted without override.
</default-conventions>
