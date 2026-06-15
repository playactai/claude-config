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

from skills.planner.quality_reviewer.qr_verify_base import VerifyBase
from skills.planner.shared.qr.phases import get_phase_config

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
  - Orphan milestones (no code_intents)
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
# One VerifyBase subclass per phase: the only per-phase behavior is
# get_verification_guidance (scope/check -> instruction lines). Kept as classes
# (not flattened to data) so the bodies stay byte-identical to the old files.


class PlanDesignVerify(VerifyBase):
    """QR verification for plan-design phase."""

    PHASE = "plan-design"

    def get_verification_guidance(self, item: dict, state_dir: str) -> list[str]:
        """Plan-design-specific verification instructions."""
        scope = item.get("scope", "*")
        check = item.get("check", "")

        guidance = []

        if scope == "*":
            # Macro check
            guidance.extend(
                [
                    "MACRO CHECK - Verify across entire plan.json:",
                    "",
                    "  Read plan.json:",
                    f"    cat {state_dir}/plan.json | jq '.'",
                    "",
                ]
            )
        elif scope.startswith("milestone:"):
            milestone_id = scope.split(":", 1)[1]
            guidance.extend(
                [
                    f"MILESTONE CHECK - Focus on {milestone_id}:",
                    "",
                    "  Read milestone:",
                    f"    cat {state_dir}/plan.json | jq '.milestones[] | select(.id == \"{milestone_id}\")'",
                    "",
                ]
            )
        elif scope.startswith("code_intent:"):
            intent_id = scope.split(":", 1)[1]
            guidance.extend(
                [
                    f"CODE INTENT CHECK - Focus on {intent_id}:",
                    "",
                    "  Read intent (find containing milestone first):",
                    f"    cat {state_dir}/plan.json | jq '.milestones[].code_intents[] | select(.id == \"{intent_id}\")'",
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

        # Add check-specific guidance
        if "decision_log" in check.lower() or "decision log" in check.lower():
            guidance.extend(
                [
                    "DECISION LOG VERIFICATION:",
                    "  - Each entry should have multi-step reasoning",
                    "  - BAD: 'Polling | Webhooks unreliable'",
                    "  - GOOD: 'Polling | 30% webhook failure -> need fallback anyway'",
                    "",
                ]
            )
        elif "policy" in check.lower():
            guidance.extend(
                [
                    "POLICY DEFAULT VERIFICATION:",
                    "  - Policy defaults affect user/org (lifecycle, capacity, failure handling)",
                    "  - Must have Tier 1 (user-specified) backing in decision_log",
                    "  - Technical defaults can use Tier 2-3 backing",
                    "",
                ]
            )
        elif "code_intent" in check.lower():
            guidance.extend(
                [
                    "CODE INTENT VERIFICATION:",
                    "  - Each implementation milestone needs code_intents",
                    "  - Each code_intent needs file path and behavior",
                    "  - decision_refs should point to valid decision_log entries",
                    "",
                ]
            )

        return guidance


class ImplCodeVerify(VerifyBase):
    """QR verification for impl-code phase."""

    PHASE = "impl-code"

    def get_verification_guidance(self, item: dict, state_dir: str) -> list[str]:
        """Impl-code-specific verification instructions."""
        scope = item.get("scope", "*")
        check = item.get("check", "")

        guidance = []

        if scope == "*":
            guidance.extend(
                [
                    "MACRO CHECK - Verify across all implemented code:",
                    "",
                    "  Read plan.json for acceptance criteria (code milestones only --",
                    "  is_documentation_only milestones have no implemented code and are",
                    "  verified in impl-docs):",
                    f"    cat {state_dir}/plan.json | jq '.milestones[] | select(.is_documentation_only != true) | .acceptance_criteria'",
                    "",
                    "  Read modified files from codebase.",
                    "",
                ]
            )
        elif scope.startswith("milestone:"):
            ms_id = scope.split(":", 1)[1]
            guidance.extend(
                [
                    f"MILESTONE CHECK - Focus on {ms_id}:",
                    "",
                    "  Extract milestone:",
                    f"    cat {state_dir}/plan.json | jq '.milestones[] | select(.id == \"{ms_id}\")'",
                    "",
                    "  Read the files associated with this milestone.",
                    "",
                ]
            )
        elif scope.startswith("file:"):
            file_path = scope.split(":", 1)[1]
            guidance.extend(
                [
                    f"FILE CHECK - Focus on {file_path}:",
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
        if "factored" in check.lower() and "expect" in check.lower():
            guidance.extend(
                [
                    "FACTORED VERIFICATION - STEP 1 (Expectations):",
                    "  Write down what you EXPECT to observe in code",
                    "  BEFORE reading the actual implementation.",
                    "  | Criterion | Expected Code Evidence |",
                    "  | --------- | ---------------------- |",
                    "  Fill this table FIRST, then proceed to observation step.",
                    "",
                ]
            )
        elif "factored" in check.lower() and "actually" in check.lower():
            guidance.extend(
                [
                    "FACTORED VERIFICATION - STEP 2 (Observations):",
                    "  Document what the code ACTUALLY does",
                    "  WITHOUT re-reading acceptance criteria.",
                    "  | Function/Section | What It Actually Does |",
                    "  | ---------------- | --------------------- |",
                    "  Note behaviors, not what it should do.",
                    "",
                ]
            )
        elif "factored" in check.lower() and "compare" in check.lower():
            guidance.extend(
                [
                    "FACTORED VERIFICATION - STEP 3 (Comparison):",
                    "  NOW compare your expectations vs observations.",
                    "  | Criterion | Expected | Observed | Match? |",
                    "  | --------- | -------- | -------- | ------ |",
                    "  Report mismatches as FAIL.",
                    "",
                ]
            )
        elif "marker" in check.lower() or ":perf:" in check.lower() or ":unsafe:" in check.lower():
            guidance.extend(self._intent_marker_guidance(include_examples=True))
        elif "temporal" in check.lower():
            guidance.extend(self._temporal_contamination_guidance())
        elif "god function" in check.lower() or "nesting" in check.lower():
            guidance.extend(
                [
                    "STRUCTURAL CHECK:",
                    "  - No functions >50 lines",
                    "  - No nesting >3 levels",
                    "  Count lines and nesting depth for flagged functions.",
                    "",
                ]
            )
        elif "duplicate" in check.lower():
            guidance.extend(
                [
                    "DUPLICATION CHECK:",
                    "  Look for copy-pasted code blocks",
                    "  or parallel functions doing similar things.",
                    "",
                ]
            )
        elif "code quality" in check.lower():
            guidance.extend(
                [
                    "CODE QUALITY CHECK:",
                    "  Apply all 8 quality documents:",
                    "  01-naming, 02-structure, 03-patterns, 04-repetition,",
                    "  05-documentation, 06-module, 07-cross-file, 08-codebase",
                    "",
                ]
            )

        return guidance


class ImplDocsVerify(VerifyBase):
    """QR verification for impl-docs phase."""

    PHASE = "impl-docs"

    def get_verification_guidance(self, item: dict, state_dir: str) -> list[str]:
        """Impl-docs-specific verification instructions."""
        scope = item.get("scope", "*")
        check = item.get("check", "")

        guidance = []

        if scope == "*":
            guidance.extend(
                [
                    "MACRO CHECK - Verify across all documentation:",
                    "",
                    "  Read plan.json for IK and modified files:",
                    f"    cat {state_dir}/plan.json | jq '{{ik: .invisible_knowledge, milestones: .milestones[].files}}'",
                    "",
                    "  Read documentation-only milestone deliverables (files + acceptance criteria):",
                    f"    cat {state_dir}/plan.json | jq '.milestones[] | select(.is_documentation_only == true) | {{files, acceptance_criteria}}'",
                    "",
                    "  Read CLAUDE.md and README.md files in modified directories.",
                    "",
                ]
            )
        elif scope.startswith("milestone:"):
            ms_id = scope.split(":", 1)[1]
            guidance.extend(
                [
                    f"MILESTONE CHECK - Focus on {ms_id}:",
                    "",
                    "  Extract the milestone (files + acceptance criteria):",
                    f"    cat {state_dir}/plan.json | jq '.milestones[] | select(.id == \"{ms_id}\")'",
                    "",
                    "  Read the files this milestone authored and verify its acceptance criteria.",
                    "",
                ]
            )
        elif scope.startswith("directory:"):
            directory = scope.split(":", 1)[1]
            guidance.extend(
                [
                    f"DIRECTORY CHECK - Focus on {directory}:",
                    "",
                    f"  Read CLAUDE.md: cat {directory}/CLAUDE.md",
                    f"  Read README.md: cat {directory}/README.md (if exists)",
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
        if "claude.md" in check.lower() and "tabular" in check.lower():
            guidance.extend(
                [
                    "CLAUDE.MD FORMAT CHECK:",
                    "  Must use tabular index format:",
                    "  | File | Contents (WHAT) | Read When (WHEN) |",
                    "  | ---- | --------------- | ---------------- |",
                    "  - FAIL if prose instead of table",
                    "  - FAIL if overview >1 sentence",
                    "",
                ]
            )
        elif "forbidden section" in check.lower():
            guidance.extend(
                [
                    "FORBIDDEN SECTIONS CHECK:",
                    "  CLAUDE.md must NOT have:",
                    "  - 'Key Invariants' section",
                    "  - 'Dependencies' section",
                    "  - 'Constraints' section",
                    "  These belong in README.md, not CLAUDE.md.",
                    "",
                ]
            )
        elif "overview" in check.lower() and "one sentence" in check.lower():
            guidance.extend(
                [
                    "OVERVIEW LENGTH CHECK:",
                    "  CLAUDE.md overview must be ONE sentence max.",
                    "  Count sentences in Overview section.",
                    "",
                ]
            )
        elif "temporal" in check.lower():
            guidance.extend(self._temporal_contamination_guidance())
        elif "ik" in check.lower() and "proximity" in check.lower():
            guidance.extend(
                [
                    "IK PROXIMITY CHECK:",
                    "  Each Invisible Knowledge item must be documented",
                    "  in README.md in the SAME directory as affected code.",
                    "  - FAIL if IK is in separate doc/ directory",
                    "  - FAIL if IK references external wiki without local summary",
                    "",
                ]
            )
        elif "readme" in check.lower() and "created" in check.lower():
            guidance.extend(
                [
                    "README.MD CREATION CHECK:",
                    "  If invisible_knowledge has content:",
                    "  - README.md should exist in relevant directories",
                    "  - README.md should contain IK items",
                    "",
                ]
            )
        elif "self-contained" in check.lower():
            guidance.extend(
                [
                    "SELF-CONTAINED CHECK:",
                    "  README.md must not rely on external sources:",
                    "  - No 'see wiki for details'",
                    "  - No 'refer to doc/ directory'",
                    "  External knowledge must be summarized locally.",
                    "",
                ]
            )
        elif "deliverable" in check.lower() or "acceptance" in check.lower():
            guidance.extend(
                [
                    "DOCUMENTATION-ONLY DELIVERABLE CHECK:",
                    "  For the documentation-only milestone, read each file in its files[]",
                    "  and confirm every acceptance criterion is satisfied by the authored docs.",
                    "  - A milestone with NO acceptance_criteria is vacuously satisfied (PASS).",
                    "  - FAIL a criterion only with concrete evidence it is unmet.",
                    "",
                ]
            )
        elif "marker" in check.lower():
            guidance.extend(self._intent_marker_guidance(include_examples=False))

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
