"""Phase-specific QR content for the parameterized decompose/verify runners.

Single home for the per-phase content that used to live in six near-identical
files (`{plan_design,impl_code,impl_docs}_qr_{decompose,verify}.py`). The
`qr_decompose.py` / `qr_verify.py` runners select by `--phase`:

- DECOMPOSE: phase-specific cognitive prompts (steps 1-3, 5) + grouping examples,
  registered in DECOMPOSE_CONTENT. The 13-step control flow stays in
  prompts/decompose.dispatch_step; this module only supplies content.
- VERIFY: one VerifyBase subclass per phase (the only per-phase behavior is
  get_verification_guidance), registered in VERIFIERS. Shared step routing / CLI
  wiring / result recording stay in qr_verify_base.

The prompt blocks and guidance bodies are relocated VERBATIM from the old files
(triple-quoted to preserve exact text -- tests assert on substrings/offsets).
Constants are phase-prefixed ([PHASE]_[TYPE]) so the three phases coexist here.
"""

import json

from skills.planner.quality_reviewer.prompts.decompose import no_scope, render_code_milestone_scope
from skills.planner.quality_reviewer.qr_verify_base import (
    VerifyBase,
    parse_scope,
    select_check_guidance,
)
from skills.planner.shared.builders import shell_quote
from skills.planner.shared.qr.phases import get_phase_config
from skills.planner.shared.schema import CODE_ONLY_SELECTOR, DOC_ONLY_DELIVERABLES_FILTER


def _jq_command(state_dir: str, jq_filter: str) -> str:
    """Shell-safe `cat plan.json | jq <filter>` for the verifier to copy-run."""
    return f"cat {shell_quote(state_dir)}/plan.json | jq {shell_quote(jq_filter)}"


def _jq_select_by_id(state_dir: str, selector: str, item_id: str) -> str:
    """As _jq_command, selecting an entity by id. json.dumps -> safe jq string
    literal (blocks jq-program injection); shell_quote of the whole filter blocks
    shell injection incl. a single-quote in a scope-derived id (json.dumps alone
    is NOT shell-safe; shell_quote alone is NOT jq-safe — both layers needed)."""
    return _jq_command(state_dir, f"{selector} | select(.id == {json.dumps(item_id)})")

# ============================================================================
# DECOMPOSE PROMPTS
# ============================================================================

# --- plan-design ------------------------------------------------------------

PLAN_DESIGN_STEP_1_ABSORB = """\
Read plan.json from STATE_DIR:
  cat $STATE_DIR/plan.json | jq '.'

SCOPE: Plan structure and decision quality.

Focus on:
  - planning_context.decisions (completeness, reasoning quality)
  - planning_context.constraints (all documented?)
  - planning_context.risks (identified and addressed?)
  - milestones[].code_intents (structure present?)
  - invisible_knowledge (captured?)

OUT OF SCOPE (verified at execution):
  - Code correctness (impl-code phase)
  - Documentation quality (impl-docs phase)"""


PLAN_DESIGN_STEP_2_CONCERNS = """\
Brainstorm concerns specific to PLAN STRUCTURE:
  - Missing decisions (non-obvious choices not logged)
  - Policy defaults without user backing
  - Orphan code milestones (no code_intents, excluding is_documentation_only)
  - Invalid references (decision_refs point nowhere)
  - Reasoning chains too shallow
  - Risks identified but not addressed

DO NOT brainstorm code or documentation concerns (out of scope)."""


PLAN_DESIGN_STEP_3_ENUMERATION = """\
For plan-design, enumerate PLAN STRUCTURE ARTIFACTS:

DECISIONS:
  - Each decision in planning_context.decisions (ID, decision text)
  - Has reasoning? Multi-step chain?

CONSTRAINTS:
  - Each constraint in planning_context.constraints (ID, type)
  - User-specified or inferred?

RISKS:
  - Each risk in planning_context.risks (ID, risk text)
  - Has mitigation?

MILESTONES:
  - Each milestone (ID, name, count of code_intents)
  - Each code_intent with decision_refs (ID, which decisions referenced)

INVISIBLE KNOWLEDGE:
  - system, invariants[], tradeoffs[] content"""


PLAN_DESIGN_STEP_5_GENERATE = """\
SEVERITY ASSIGNMENT (per conventions/severity.md, plan-design scope):

  MUST (blocks all iterations):
    - DIAGRAM categories:
      * ORPHAN_NODE: node with zero edges
      * INVALID_EDGE_REF: edge references missing node
      * INVALID_SCOPE_REF: scope references non-existent milestone
    - KNOWLEDGE subset:
      * DECISION_LOG_MISSING: non-trivial choice without logged rationale
      * POLICY_UNJUSTIFIED: policy default without Tier 1 backing
      * ASSUMPTION_UNVALIDATED: architectural assumption without citation

  SHOULD (iterations 1-3):
    - Shallow reasoning chains (premise without implication)
    - Missing risk mitigations
    - Incomplete constraint documentation

  COULD (iterations 1-2):
    - Cosmetic plan formatting
    - Minor inconsistencies in naming"""


PLAN_DESIGN_COMPONENT_EXAMPLES = """\
  - A milestone
  - A major decision
  - A constraint category"""


PLAN_DESIGN_CONCERN_EXAMPLES = """\
  - Reasoning chain quality
  - Reference integrity
  - Risk coverage"""


# --- impl-code --------------------------------------------------------------

IMPL_CODE_STEP_1_ABSORB = """\
Read plan.json from STATE_DIR:
  cat $STATE_DIR/plan.json | jq '.'

Also read MODIFIED_FILES from codebase (paths from milestones).

SCOPE: Implemented code quality.

Focus on:
  - milestones[].acceptance_criteria (expectations)
  - Actual implemented code in modified files (observations)
  - Code quality (structure, patterns, documentation)
  - Intent markers in implemented code

OUT OF SCOPE:
  - Plan structure (already verified in plan-design)
  - Documentation files (impl-docs phase)
  - is_documentation_only milestones (no code is implemented for them; their
    acceptance criteria are documentation deliverables verified in impl-docs)"""


IMPL_CODE_STEP_2_CONCERNS = """\
Brainstorm concerns specific to IMPLEMENTED CODE:
  - Acceptance criteria not met
  - Cross-cutting concerns broken (error handling, logging)
  - Code quality violations (god objects, god functions)
  - Missing or invalid intent markers
  - Implementation drift from plan

DO NOT brainstorm plan structure or documentation concerns."""


IMPL_CODE_STEP_3_ENUMERATION = """\
For impl-code, enumerate IMPLEMENTATION ARTIFACTS.
Enumerate items ONLY for the code milestones listed under "CODE MILESTONES IN
SCOPE" below; is_documentation_only milestones are excluded by construction (no
code is implemented for them -- their deliverables are verified in impl-docs).
Enumerating a doc-only milestone produces unsatisfiable acceptance-criteria
items that never converge.

ACCEPTANCE CRITERIA (code milestones only):
  - Each milestone with acceptance_criteria (ID, criteria count)
  - Each criterion (ID, expectation text)

FILES:
  - Files modified per milestone (path list)
  - Actual file content (read from codebase)

CROSS-CUTTING:
  - Error handling patterns used
  - Logging patterns used
  - Shared state access patterns

CODE QUALITY:
  - Function sizes (line counts)
  - Nesting depths
  - Dependency counts"""


IMPL_CODE_STEP_5_GENERATE = """\
SEVERITY ASSIGNMENT (per conventions/severity.md, impl-code scope):

  MUST (blocks all iterations):
    - Acceptance criterion not met
    - MARKER_INVALID: intent marker without valid explanation

  SHOULD (iterations 1-3) - STRUCTURE categories:
    - GOD_OBJECT: >15 methods OR >10 deps
    - GOD_FUNCTION: >50 lines OR >3 nesting
    - CONVENTION_VIOLATION: violates documented project convention
    - TESTING_STRATEGY_VIOLATION: tests don't follow confirmed strategy
    - INCONSISTENT_ERROR_HANDLING: mixed exceptions/codes

  SHOULD (iterations 1-3) - STRUCTURAL SIMPLIFICATION (flag only with a concrete, behavior-preserving fix named):
    - MISSED_SIMPLIFICATION: restructuring would delete branches/helpers/layers
    - FILE_SIZE_EXPLOSION: diff grows a file past 1000 lines without decomposition
    - SPAGHETTI_CONDITIONAL: ad-hoc special-case branch bolted onto an existing/shared flow
    - THIN_ABSTRACTION: identity wrapper / pass-through adding indirection, not clarity
    - BOUNDARY_TYPE_EROSION: needless cast/any/unknown/optional papering over an invariant
    - CANONICAL_DUPLICATION: bespoke near-duplicate of an existing canonical helper
    - LAYER_LEAK: feature logic in a shared path OR logic in the wrong layer
    - NON_ATOMIC_ORCHESTRATION: avoidable serialization OR half-applied partial-update state

  COULD (iterations 1-2) - COSMETIC:
    - TOOLCHAIN_CATCHABLE: errors the compiler/linter would flag
    - DEAD_CODE: unused functions, impossible branches
    - FORMATTER_FIXABLE: style issues"""


IMPL_CODE_COMPONENT_EXAMPLES = """\
  - A modified file
  - A milestone's implementation
  - A cross-cutting concern pattern"""


IMPL_CODE_CONCERN_EXAMPLES = """\
  - Acceptance criteria compliance
  - Error handling consistency
  - Code structure quality"""


# --- impl-docs --------------------------------------------------------------

IMPL_DOCS_STEP_1_ABSORB = """\
Read plan.json from STATE_DIR:
  cat $STATE_DIR/plan.json | jq '.'

Also read documentation files in modified directories:
  - CLAUDE.md files
  - README.md files
  - Comments in source files

SCOPE: Post-implementation documentation quality.

Focus on:
  - invisible_knowledge section (was it transferred?)
  - Modified directory list (need docs?)
  - CLAUDE.md format compliance
  - README.md presence where required
  - documentation-only milestones (files authored? acceptance_criteria satisfied?)

OUT OF SCOPE:
  - Code quality (verified in impl-code)
  - Plan structure (verified in plan-design)"""


IMPL_DOCS_STEP_2_CONCERNS = """\
Brainstorm concerns specific to POST-IMPL DOCUMENTATION:
  - CLAUDE.md missing or wrong format (tabular index required)
  - IK not at best location (should be adjacent to code)
  - Temporal contamination in comments
  - README.md missing where required
  - Comments don't explain WHY
  - Documentation-only milestone deliverable missing or acceptance criteria unsatisfied

DO NOT brainstorm code quality or plan structure concerns."""


IMPL_DOCS_STEP_3_ENUMERATION = """\
For impl-docs, enumerate DOCUMENTATION ARTIFACTS:

DIRECTORIES:
  - Each directory with modified files (directory path)
  - CLAUDE.md exists? Format correct?
  - README.md exists where required?

INVISIBLE KNOWLEDGE:
  - Each invisible_knowledge item (count, topics)
  - Current location vs best location

COMMENTS:
  - Source files with new comments
  - Temporal contamination candidates

DOCUMENTATION-ONLY MILESTONES:
  - Each is_documentation_only milestone (files, acceptance_criteria count)
  - Each acceptance criterion (expectation text) -- the deliverable it requires"""


IMPL_DOCS_STEP_5_GENERATE = """\
SEVERITY ASSIGNMENT (per conventions/severity.md, impl-docs scope):

  MUST (blocks all iterations) - knowledge loss & planned-deliverable completeness:
    - IK_TRANSFER_FAILURE: invisible knowledge not at best location
    - TEMPORAL_CONTAMINATION: change-relative language in comments
    - BASELINE_REFERENCE: comment references removed code
    - DOC_DELIVERABLE_UNSATISFIED: a documentation-only milestone's acceptance
      criteria not met by its authored files. The planned doc that does not
      exist IS the knowledge loss; at the iteration ceiling this escalates to
      the user rather than finalizing the plan with the deliverable missing.

  SHOULD (iterations 1-3):
    - CLAUDE.md format violations
    - README.md missing where scope warrants
    - WHY-not-WHAT violations

  COULD (iterations 1-2):
    - Minor formatting inconsistencies
    - Documentation style variations"""


IMPL_DOCS_COMPONENT_EXAMPLES = """\
  - A modified directory
  - A CLAUDE.md file
  - A README.md file"""


IMPL_DOCS_CONCERN_EXAMPLES = """\
  - IK proximity
  - Temporal contamination
  - Format compliance"""


# ============================================================================
# DECOMPOSE CONTENT REGISTRY
# ============================================================================
#
# phase -> {phase_prompts: {step: prompt}, grouping_config: {*_examples}}.
# Keys mirror the old per-file PHASE_PROMPTS / GROUPING_CONFIG dicts, so
# dispatch_step is called identically -- only the lookup moved here.

DECOMPOSE_CONTENT: dict[str, dict] = {
    "plan-design": {
        "phase_prompts": {
            1: PLAN_DESIGN_STEP_1_ABSORB,
            2: PLAN_DESIGN_STEP_2_CONCERNS,
            3: PLAN_DESIGN_STEP_3_ENUMERATION,
            5: PLAN_DESIGN_STEP_5_GENERATE,
        },
        "grouping_config": {
            "component_examples": PLAN_DESIGN_COMPONENT_EXAMPLES,
            "concern_examples": PLAN_DESIGN_CONCERN_EXAMPLES,
        },
        "scope_provider": no_scope,
    },
    "impl-code": {
        "phase_prompts": {
            1: IMPL_CODE_STEP_1_ABSORB,
            2: IMPL_CODE_STEP_2_CONCERNS,
            3: IMPL_CODE_STEP_3_ENUMERATION,
            5: IMPL_CODE_STEP_5_GENERATE,
        },
        "grouping_config": {
            "component_examples": IMPL_CODE_COMPONENT_EXAMPLES,
            "concern_examples": IMPL_CODE_CONCERN_EXAMPLES,
        },
        "scope_provider": render_code_milestone_scope,
    },
    "impl-docs": {
        "phase_prompts": {
            1: IMPL_DOCS_STEP_1_ABSORB,
            2: IMPL_DOCS_STEP_2_CONCERNS,
            3: IMPL_DOCS_STEP_3_ENUMERATION,
            5: IMPL_DOCS_STEP_5_GENERATE,
        },
        "grouping_config": {
            "component_examples": IMPL_DOCS_COMPONENT_EXAMPLES,
            "concern_examples": IMPL_DOCS_CONCERN_EXAMPLES,
        },
        "scope_provider": no_scope,
    },
}


def get_decompose_content(phase: str) -> dict:
    """Phase-specific decompose prompts + grouping examples.

    Raises ValueError on an unknown phase (matches get_phase_config / get_verifier).
    """
    get_phase_config(phase)
    return DECOMPOSE_CONTENT[phase]


# ============================================================================
# VERIFY CLASSES
# ============================================================================
#
# One VerifyBase subclass per phase; the only per-phase behavior is
# get_verification_guidance. Scope dispatch is shared via parse_scope; the
# check-specific guidance is an ordered (predicate, lines) table resolved by
# select_check_guidance (first match wins). Emitted lines are verbatim -- tests
# assert on substrings/offsets.


class PlanDesignVerify(VerifyBase):
    """QR verification for plan-design phase."""

    PHASE = "plan-design"

    def get_verification_guidance(self, item: dict, state_dir: str) -> list[str]:
        """Plan-design-specific verification instructions."""
        scope = item.get("scope", "*")
        check = item.get("check", "")

        guidance = []

        kind, value = parse_scope(scope)
        if kind == "macro":
            # Macro check
            guidance.extend(
                [
                    "MACRO CHECK - Verify across entire plan.json:",
                    "",
                    "  Read plan.json:",
                    f"    {_jq_command(state_dir, '.')}",
                    "",
                ]
            )
        elif kind == "milestone":
            guidance.extend(
                [
                    f"MILESTONE CHECK - Focus on {value}:",
                    "",
                    "  Read milestone:",
                    f"    {_jq_select_by_id(state_dir, '.milestones[]', value)}",
                    "",
                ]
            )
        elif kind == "code_intent":
            guidance.extend(
                [
                    f"CODE INTENT CHECK - Focus on {value}:",
                    "",
                    "  Read intent (find containing milestone first):",
                    f"    {_jq_select_by_id(state_dir, '.milestones[].code_intents[]', value)}",
                    "",
                ]
            )
        else:
            guidance.extend(
                [
                    f"SCOPED CHECK - Scope: {scope}",
                    "",
                    "  Read the relevant section from plan.json.",
                    "",
                ]
            )

        rules = [
            (
                lambda c: "decision_log" in c or "decision log" in c,
                [
                    "DECISION LOG VERIFICATION:",
                    "  - Each entry should have multi-step reasoning",
                    "  - BAD: 'Polling | Webhooks unreliable'",
                    "  - GOOD: 'Polling | 30% webhook failure -> need fallback anyway'",
                    "",
                ],
            ),
            (
                lambda c: "policy" in c,
                [
                    "POLICY DEFAULT VERIFICATION:",
                    "  - Policy defaults affect user/org (lifecycle, capacity, failure handling)",
                    "  - Must have Tier 1 (user-specified) backing in decision_log",
                    "  - Technical defaults can use Tier 2-3 backing",
                    "",
                ],
            ),
            (
                lambda c: "code_intent" in c,
                [
                    "CODE INTENT VERIFICATION:",
                    "  - Each implementation milestone needs code_intents",
                    "  - Each code_intent needs file path and behavior",
                    "  - decision_refs should point to valid decision_log entries",
                    "",
                ],
            ),
        ]
        guidance.extend(select_check_guidance(check, rules))

        return guidance


class ImplCodeVerify(VerifyBase):
    """QR verification for impl-code phase."""

    PHASE = "impl-code"

    def get_verification_guidance(self, item: dict, state_dir: str) -> list[str]:
        """Impl-code-specific verification instructions."""
        scope = item.get("scope", "*")
        check = item.get("check", "")

        guidance = []

        kind, value = parse_scope(scope)
        if kind == "macro":
            guidance.extend(
                [
                    "MACRO CHECK - Verify across all implemented code:",
                    "",
                    "  Read plan.json for acceptance criteria (code milestones only --",
                    "  is_documentation_only milestones have no implemented code and are",
                    "  verified in impl-docs):",
                    f"    {_jq_command(state_dir, CODE_ONLY_SELECTOR + ' | .acceptance_criteria')}",
                    "",
                    "  Read modified files from codebase.",
                    "",
                ]
            )
        elif kind == "milestone":
            guidance.extend(
                [
                    f"MILESTONE CHECK - Focus on {value}:",
                    "",
                    "  Extract milestone:",
                    f"    {_jq_select_by_id(state_dir, '.milestones[]', value)}",
                    "",
                    "  Read the files associated with this milestone.",
                    "",
                ]
            )
        elif kind == "file":
            guidance.extend(
                [
                    f"FILE CHECK - Focus on {value}:",
                    "",
                    "  Read the file content from codebase.",
                    "",
                ]
            )
        else:
            guidance.extend(
                [
                    f"SCOPED CHECK - Scope: {scope}",
                    "",
                    "  Read the relevant code from codebase.",
                    "",
                ]
            )

        # Add check-specific guidance
        rules = [
            (
                lambda c: "factored" in c and "expect" in c,
                [
                    "FACTORED VERIFICATION - STEP 1 (Expectations):",
                    "  Write down what you EXPECT to observe in code",
                    "  BEFORE reading the actual implementation.",
                    "  | Criterion | Expected Code Evidence |",
                    "  | --------- | ---------------------- |",
                    "  Fill this table FIRST, then proceed to observation step.",
                    "",
                ],
            ),
            (
                lambda c: "factored" in c and "actually" in c,
                [
                    "FACTORED VERIFICATION - STEP 2 (Observations):",
                    "  Document what the code ACTUALLY does",
                    "  WITHOUT re-reading acceptance criteria.",
                    "  | Function/Section | What It Actually Does |",
                    "  | ---------------- | --------------------- |",
                    "  Note behaviors, not what it should do.",
                    "",
                ],
            ),
            (
                lambda c: "factored" in c and "compare" in c,
                [
                    "FACTORED VERIFICATION - STEP 3 (Comparison):",
                    "  NOW compare your expectations vs observations.",
                    "  | Criterion | Expected | Observed | Match? |",
                    "  | --------- | -------- | -------- | ------ |",
                    "  Report mismatches as FAIL.",
                    "",
                ],
            ),
            (
                lambda c: "marker" in c or ":perf:" in c or ":unsafe:" in c,
                lambda: self._intent_marker_guidance(include_examples=True),
            ),
            (
                lambda c: "temporal" in c,
                self._temporal_contamination_guidance,
            ),
            (
                lambda c: "god function" in c or "nesting" in c,
                [
                    "STRUCTURAL CHECK:",
                    "  - No functions >50 lines",
                    "  - No nesting >3 levels",
                    "  Count lines and nesting depth for flagged functions.",
                    "",
                ],
            ),
            (
                lambda c: "duplicate" in c,
                [
                    "DUPLICATION CHECK:",
                    "  Look for copy-pasted code blocks",
                    "  or parallel functions doing similar things.",
                    "",
                ],
            ),
            (
                lambda c: "code quality" in c,
                [
                    "CODE QUALITY CHECK:",
                    "  Apply all 8 quality documents:",
                    "  01-naming, 02-structure, 03-patterns, 04-repetition,",
                    "  05-documentation, 06-module, 07-cross-file, 08-codebase",
                    "",
                ],
            ),
            (
                lambda c: "convention" in c,
                [
                    "CONVENTION VERIFICATION:",
                    "  Confirm the change follows the documented project convention.",
                    "  - Read the relevant CLAUDE.md / conventions doc for the rule.",
                    "  - FAIL only when the code contradicts a convention that is",
                    "    actually documented (cite it); style preference is not a FAIL.",
                    "",
                ],
            ),
            (
                lambda c: "testing" in c and "strategy" in c,
                [
                    "TESTING-STRATEGY VERIFICATION:",
                    "  Confirm new/changed tests follow the strategy confirmed in the plan.",
                    "  - Check tests target the agreed level (unit/integration/property).",
                    "  - FAIL if tests assert only the happy path when the strategy",
                    "    requires edge/error coverage, or use a banned mocking style.",
                    "",
                ],
            ),
        ]
        guidance.extend(select_check_guidance(check, rules))

        return guidance


class ImplDocsVerify(VerifyBase):
    """QR verification for impl-docs phase."""

    PHASE = "impl-docs"

    def get_verification_guidance(self, item: dict, state_dir: str) -> list[str]:
        """Impl-docs-specific verification instructions."""
        scope = item.get("scope", "*")
        check = item.get("check", "")

        guidance = []

        kind, value = parse_scope(scope)
        if kind == "macro":
            guidance.extend(
                [
                    "MACRO CHECK - Verify across all documentation:",
                    "",
                    "  Read plan.json for IK and modified files:",
                    f"    {_jq_command(state_dir, '{ik: .invisible_knowledge, milestones: .milestones[].files}')}",
                    "",
                    "  Read documentation-only milestone deliverables (files + acceptance criteria):",
                    f"    {_jq_command(state_dir, DOC_ONLY_DELIVERABLES_FILTER)}",
                    "",
                    "  Read CLAUDE.md and README.md files in modified directories.",
                    "",
                ]
            )
        elif kind == "milestone":
            guidance.extend(
                [
                    f"MILESTONE CHECK - Focus on {value}:",
                    "",
                    "  Extract the milestone (files + acceptance criteria):",
                    f"    {_jq_select_by_id(state_dir, '.milestones[]', value)}",
                    "",
                    "  Read the files this milestone authored and verify its acceptance criteria.",
                    "",
                ]
            )
        elif kind == "directory":
            guidance.extend(
                [
                    f"DIRECTORY CHECK - Focus on {value}:",
                    "",
                    f"  Read CLAUDE.md: cat {shell_quote(value)}/CLAUDE.md",
                    f"  Read README.md: cat {shell_quote(value)}/README.md (if exists)",
                    "",
                ]
            )
        else:
            guidance.extend(
                [
                    f"SCOPED CHECK - Scope: {scope}",
                    "",
                    "  Read the relevant documentation files.",
                    "",
                ]
            )

        # Add check-specific guidance
        rules = [
            (
                lambda c: "claude.md" in c and "tabular" in c,
                [
                    "CLAUDE.MD FORMAT CHECK:",
                    "  Must use tabular index format:",
                    "  | File | Contents (WHAT) | Read When (WHEN) |",
                    "  | ---- | --------------- | ---------------- |",
                    "  - FAIL if prose instead of table",
                    "  - FAIL if overview >1 sentence",
                    "",
                ],
            ),
            (
                lambda c: "forbidden section" in c,
                [
                    "FORBIDDEN SECTIONS CHECK:",
                    "  CLAUDE.md must NOT have:",
                    "  - 'Key Invariants' section",
                    "  - 'Dependencies' section",
                    "  - 'Constraints' section",
                    "  These belong in README.md, not CLAUDE.md.",
                    "",
                ],
            ),
            (
                lambda c: "overview" in c and "one sentence" in c,
                [
                    "OVERVIEW LENGTH CHECK:",
                    "  CLAUDE.md overview must be ONE sentence max.",
                    "  Count sentences in Overview section.",
                    "",
                ],
            ),
            (
                lambda c: "temporal" in c,
                self._temporal_contamination_guidance,
            ),
            (
                lambda c: "ik" in c and "proximity" in c,
                [
                    "IK PROXIMITY CHECK:",
                    "  Each Invisible Knowledge item must be documented",
                    "  in README.md in the SAME directory as affected code.",
                    "  - FAIL if IK is in separate doc/ directory",
                    "  - FAIL if IK references external wiki without local summary",
                    "",
                ],
            ),
            (
                lambda c: "readme" in c and "created" in c,
                [
                    "README.MD CREATION CHECK:",
                    "  If invisible_knowledge has content:",
                    "  - README.md should exist in relevant directories",
                    "  - README.md should contain IK items",
                    "",
                ],
            ),
            (
                lambda c: "self-contained" in c,
                [
                    "SELF-CONTAINED CHECK:",
                    "  README.md must not rely on external sources:",
                    "  - No 'see wiki for details'",
                    "  - No 'refer to doc/ directory'",
                    "  External knowledge must be summarized locally.",
                    "",
                ],
            ),
            (
                lambda c: "deliverable" in c or "acceptance" in c,
                [
                    "DOCUMENTATION-ONLY DELIVERABLE CHECK:",
                    "  For the documentation-only milestone, read each file in its files[]",
                    "  and confirm every acceptance criterion is satisfied by the authored docs.",
                    "  - A milestone with NO acceptance_criteria is vacuously satisfied (PASS).",
                    "  - FAIL a criterion only with concrete evidence it is unmet.",
                    "",
                ],
            ),
            (
                lambda c: "why" in c and "what" in c,
                [
                    "WHY-NOT-WHAT VERIFICATION:",
                    "  Comments should explain reasoning, not describe code.",
                    "  BAD: 'Added a new function' (describes action)",
                    "  GOOD: 'Mutex serializes cache access' (explains purpose)",
                    "",
                ],
            ),
            (
                lambda c: "marker" in c,
                lambda: self._intent_marker_guidance(include_examples=False),
            ),
        ]
        guidance.extend(select_check_guidance(check, rules))

        return guidance


# ============================================================================
# VERIFY REGISTRY
# ============================================================================

VERIFIERS: dict[str, type[VerifyBase]] = {
    "plan-design": PlanDesignVerify,
    "impl-code": ImplCodeVerify,
    "impl-docs": ImplDocsVerify,
}

# The DECOMPOSE_CONTENT == VERIFIERS == QR_PHASES coverage check now lives in
# phases.validate_phase_registries(), invoked from get_phase_config so a phase
# added to QR_PHASES but missing its content/verifier here fails on the eager
# routing/arg path (at startup) instead of only when this module is first imported
# mid-dispatch.


def get_verifier(phase: str) -> VerifyBase:
    """Instantiate the VerifyBase subclass for a phase.

    Raises ValueError on an unknown phase (matches get_phase_config).
    """
    get_phase_config(phase)
    return VERIFIERS[phase]()
