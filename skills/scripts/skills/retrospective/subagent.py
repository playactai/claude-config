#!/usr/bin/env python3
"""
Retrospective sub-agent — analyzes a Claude Code transcript and emits a
structured PROPOSALS JSON block for the parent orchestrator.

Four-step workflow:
  1. PARSE    - Extract events from the transcript JSONL via jq
  2. SIGNALS  - Detect correction signals (tool errors, "no/stop/wait", repeats)
  3. CLASSIFY - Bucket findings into corrections / work / improvisations / friction
  4. REPORT   - Emit the final PROPOSALS JSON block

The transcript path is supplied by the parent in the launching prompt. The
sub-agent does NOT decide which transcript to read.
"""

import argparse
import sys
from textwrap import indent

from skills.lib.workflow.core import StepDef, Workflow
from skills.lib.workflow.prompts import format_step

# ============================================================================
# CONFIGURATION
# ============================================================================

MODULE_PATH = "skills.retrospective.subagent"
TOTAL_STEPS = 4


# ============================================================================
# MESSAGE TEMPLATES
# ============================================================================

# --- STEP 1: PARSE ----------------------------------------------------------

# JSONL leaf-selection jq used by PARSE_INSTRUCTIONS. Extracted to a constant
# so tests can pipe fixtures through this exact snippet without scraping the
# prompt body.
#
# Picks the active leaf by ORIGINAL JSONL ORDER (last leaf encountered when
# scanning $all end-to-end), not by .timestamp:
#   - Transcripts are append-only; a fork writes the new branch to the file
#     tail, so the last leaf-in-file is the active branch by construction.
#   - jq has no datetime parser; lex-sorting ISO timestamps would break on
#     mixed offsets (transcript.py:166-170 uses Python to handle that).
#   - Robust to entries missing/malformed `timestamp` (iter_messages drops
#     those silently; jq would crash on null-vs-string compare).
LEAF_SELECTION_JQ = (
    "(map(select(.uuid))) as $all |\n"
    "($all | map(.uuid) | (. - (. as $u | $all | map(.parentUuid)))) as $leaves |\n"
    "($all | map({(.uuid): .parentUuid}) | add) as $parent |\n"
    "def chain(u): if u == null then [] else [u] + chain($parent[u]) end;\n"
    "([$all[] | select(.uuid as $u | $leaves | index($u))] | last | .uuid) as $leaf |\n"
    "chain($leaf) | reverse as $main_uuids |\n"
    "$all | map(select(.uuid as $u | $main_uuids | index($u)))"
)

PARSE_INSTRUCTIONS = (
    "PARSE - Extract events from the transcript JSONL.\n"
    "\n"
    "INPUT: The launching prompt contains two `KEY=value` lines you must read\n"
    "before running anything:\n"
    "  TRANSCRIPT_PATH=<absolute path to the .jsonl>\n"
    "  SINCE=<window spec — 'all', 'Nh', 'Nd', 'Nw', 'Nm', or ISO timestamp>\n"
    "Treat TRANSCRIPT_PATH as authoritative — do not search for other\n"
    "transcripts. Re-invocations of later steps take only --step.\n"
    "\n"
    "ACTIONS (use Bash + jq):\n"
    "  1. Verify the transcript file exists and is non-empty. If empty,\n"
    "     emit '{\"version\":1,\"session_id\":\"<uuid>\",\"proposals\":[]}'\n"
    "     (uuid from the transcript filename) and skip to step 4 directly.\n"
    "  2. Apply the SINCE filter from the launching prompt (default: all).\n"
    "  3. Build a compact 'events' digest with these projections:\n"
    "       - User text messages (type='user', message.content text fields)\n"
    "       - Tool calls (type='assistant', content[].type='tool_use', .name + .input)\n"
    "       - Tool results with is_error=true (type='user',\n"
    "         content[].type='tool_result', is_error=true)\n"
    "       - Assistant thinking-block lengths (type='assistant',\n"
    "         content[].type='thinking', .thinking length)\n"
    "       - Timestamps for sequencing\n"
    "  4. Keep only the main branch: if the transcript has parentUuid forks,\n"
    "     walk back from the most-recent leaf to root and emit only that\n"
    "     chain. Inline jq pattern (no external skill dependency):\n"
    "       jq -s '\n"
    + indent(LEAF_SELECTION_JQ, " " * 9)
    + "\n"
    "       ' \"$TRANSCRIPT\"\n"
    "     Most transcripts have no forks; this is a safety net for branched\n"
    "     sessions. The leaf is picked by JSONL order (last leaf in file =\n"
    "     active branch), not lexicographic UUID — see LEAF_SELECTION_JQ.\n"
    "\n"
    "OUTPUT FORMAT (free text, for your own reasoning in the next step):\n"
    "```\n"
    "EVENTS:\n"
    "  user_messages: N\n"
    "  tool_calls: N (top tools: ...)\n"
    "  tool_errors: N\n"
    "  assistant_thinking_blocks: N (longest: X chars)\n"
    "  time_range: ts_first .. ts_last\n"
    "  notable_quotes:\n"
    "    - line K: '<verbatim user text snippet>'\n"
    "    - ...\n"
    "```\n"
    "\n"
    "Keep notable_quotes to <=15 entries; pick the ones most likely to be\n"
    "corrections or strong-signal user messages."
)

# --- STEP 2: SIGNALS --------------------------------------------------------

SIGNALS_INSTRUCTIONS = (
    "SIGNALS - Detect correction signals from the events digest.\n"
    "\n"
    "Apply ALL FIVE detectors. Each finding records: signal_type, evidence\n"
    "(transcript line/uuid + verbatim quote), confidence (low|medium|high).\n"
    "\n"
    "  (a) ERROR_THEN_CORRECTION:\n"
    "      tool_result with is_error=true, immediately followed by a user\n"
    "      message that references the failure or redirects approach.\n"
    "      Confidence: high if user explicitly references the error.\n"
    "\n"
    "  (b) USER_OVERRIDE_LEXICAL:\n"
    "      user message text matches /^(no|stop|don't|wait|actually|instead)\\b/i\n"
    "      OR contains 'not what I' / 'do X instead' phrases.\n"
    "      Confidence: high for opening-word matches; medium for in-sentence.\n"
    "\n"
    "  (c) REPEATED_PROMPT:\n"
    "      consecutive (or near-consecutive) user messages with high textual\n"
    "      similarity (Levenshtein < 30% of length) — sign agent didn't\n"
    "      understand. Confidence: medium.\n"
    "\n"
    "  (d) WORK_REVERSAL:\n"
    "      Bash tool_use containing /(git reset|git revert|git checkout --|\n"
    "      --amend|rm -rf .* committed)/ shortly after a commit or write.\n"
    "      Confidence: high.\n"
    "\n"
    "  (e) THINKING_THEN_OVERRIDE:\n"
    "      assistant thinking block >500 chars immediately followed by a\n"
    "      short user message (<200 chars) overriding the path.\n"
    "      Confidence: low (noisy signal — use only as supporting evidence).\n"
    "\n"
    "OUTPUT FORMAT:\n"
    "```\n"
    "SIGNALS:\n"
    "  - id: s1\n"
    "    type: USER_OVERRIDE_LEXICAL\n"
    "    confidence: high\n"
    "    evidence: 'line 412: \"no, do X instead — Y is wrong because Z\"'\n"
    "    context: 'After agent wrote a fail-closed handler the user wanted...'\n"
    "  - id: s2\n"
    "    ...\n"
    "```"
)

# --- STEP 3: CLASSIFY -------------------------------------------------------

CLASSIFY_INSTRUCTIONS = (
    "CLASSIFY - Bucket signals + non-signal events into four categories.\n"
    "\n"
    "  USER_CORRECTIONS:\n"
    "    Signals from detectors (a), (b), and high-confidence (c). These are\n"
    "    the strongest candidates for memory entries.\n"
    "\n"
    "  AGENT_WORK:\n"
    "    Sustained tool sequences with no nearby corrections — successful\n"
    "    work the user implicitly approved. These are candidate 'project'\n"
    "    memories: facts about how the codebase works that were learned\n"
    "    during the session.\n"
    "\n"
    "  IMPROVISED_STEPS:\n"
    "    Workarounds the agent invented because no skill/rule covered the\n"
    "    case (e.g., manual jq queries because no helper exists). These\n"
    "    are candidates for 'reference' or skill-improvement memories.\n"
    "\n"
    "  FRICTION_POINTS:\n"
    "    Signals from (d) and (e). Lower-confidence; useful as evidence in\n"
    "    other proposals but rarely a proposal on their own.\n"
    "\n"
    "DEDUPE: signals targeting the same memory subject (same project fact,\n"
    "same correction theme) collapse into a single classification entry.\n"
    "\n"
    "OUTPUT FORMAT:\n"
    "```\n"
    "CLASSIFICATIONS:\n"
    "  USER_CORRECTIONS:\n"
    "    - subject: 'flask CSRF setting'\n"
    "      severity: medium\n"
    "      memory_type: feedback\n"
    "      signals: [s1, s4]\n"
    "  AGENT_WORK:\n"
    "    - subject: 'how the auth middleware composes'\n"
    "      memory_type: project\n"
    "      signals: []  # implicit success\n"
    "  IMPROVISED_STEPS:\n"
    "    - subject: 'manual transcript jq queries'\n"
    "      memory_type: reference\n"
    "      signals: [s7]\n"
    "  FRICTION_POINTS: []\n"
    "```"
)

# --- STEP 4: REPORT ---------------------------------------------------------

REPORT_INSTRUCTIONS = (
    "REPORT - Emit the final PROPOSALS JSON block for the parent.\n"
    "\n"
    "For each classification entry, draft ONE proposal. Skip entries with\n"
    "no extractable rule or fact (FRICTION_POINTS often produces nothing\n"
    "actionable).\n"
    "\n"
    "PROPOSAL FIELDS (all required unless marked):\n"
    "  id              - 'p1', 'p2', ...\n"
    "  target          - absolute path of the memory file to create/update.\n"
    "                    For new entries: ~/.claude/projects/<enc>/memory/\n"
    "                    <slug>.md  (slug = snake_case from the subject).\n"
    "                    The encoded project dir was given in your launching\n"
    "                    prompt; use it verbatim.\n"
    "  kind            - 'memory:create' | 'memory:update' | 'memory:delete'\n"
    "  memory_type     - 'user' | 'feedback' | 'project' | 'reference'\n"
    "  rationale       - one sentence: why this memory should exist. The\n"
    "                    parent fingerprints on this — keep it specific.\n"
    "  evidence        - list of transcript citations: 'line K: <quote>'\n"
    "  name            - the memory entry's frontmatter `name:` (short title)\n"
    "  description     - the memory entry's frontmatter `description:` (one\n"
    "                    line; used to decide relevance in future sessions)\n"
    "  body            - the FULL body of the memory entry, written in the\n"
    "                    structured form mandated by the user's global\n"
    "                    CLAUDE.md (rule first, then **Why:** + **How to\n"
    "                    apply:** for feedback/project types). The parent\n"
    "                    writes this verbatim.\n"
    "  severity        - 'low' | 'medium' | 'high' (default: medium)\n"
    "\n"
    "OUTPUT FORMAT (REQUIRED — single fenced JSON block, no prose around it):\n"
    "```json\n"
    "{\n"
    '  "version": 1,\n'
    '  "session_id": "<uuid from transcript filename>",\n'
    '  "proposals": [\n'
    "    {\n"
    '      "id": "p1",\n'
    '      "target": "/abs/.../memory/example.md",\n'
    '      "kind": "memory:create",\n'
    '      "memory_type": "feedback",\n'
    '      "rationale": "User corrected agent twice for X behavior; persist as a feedback rule.",\n'
    '      "evidence": ["line 412: \\"no, do X instead\\"", "line 430: \\"again, X not Y\\""],\n'
    '      "name": "X behavior should be done with Y approach",\n'
    '      "description": "Short one-line summary used for relevance lookup",\n'
    '      "body": "Lead-with-rule sentence.\\n\\n**Why:** ...\\n\\n**How to apply:** ...",\n'
    '      "severity": "medium"\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "```\n"
    "\n"
    "EMPTY CASE: if no proposals, return:\n"
    "```json\n"
    '{"version":1,"session_id":"<uuid>","proposals":[]}\n'
    "```\n"
    "\n"
    "RETURN: This is the final output to your orchestrator. Do not summarize."
)


# ============================================================================
# MESSAGE BUILDERS
# ============================================================================


def build_next_command(step: int) -> str | None:
    """Build invoke command for next step."""
    if step >= TOTAL_STEPS:
        return None
    return f"uv run python -m {MODULE_PATH} --step {step + 1}"


# ============================================================================
# STEP DEFINITIONS
# ============================================================================

STATIC_STEPS: dict[int, tuple[str, str]] = {
    1: ("Parse", PARSE_INSTRUCTIONS),
    2: ("Detect Signals", SIGNALS_INSTRUCTIONS),
    3: ("Classify", CLASSIFY_INSTRUCTIONS),
    4: ("Report", REPORT_INSTRUCTIONS),
}

WORKFLOW = Workflow(
    "retrospective-subagent",
    StepDef(id="parse", title=STATIC_STEPS[1][0], actions=[PARSE_INSTRUCTIONS]),
    StepDef(id="signals", title=STATIC_STEPS[2][0], actions=[SIGNALS_INSTRUCTIONS]),
    StepDef(id="classify", title=STATIC_STEPS[3][0], actions=[CLASSIFY_INSTRUCTIONS]),
    StepDef(id="report", title=STATIC_STEPS[4][0], actions=[REPORT_INSTRUCTIONS]),
    description="Retrospective sub-agent: parses transcript, detects signals, emits proposals JSON.",
)


# ============================================================================
# OUTPUT FORMATTING
# ============================================================================


def format_output(step: int) -> str:
    if step not in STATIC_STEPS:
        return f"ERROR: Invalid step {step}"
    title, instructions = STATIC_STEPS[step]
    next_cmd = build_next_command(step) or ""
    return format_step(instructions, next_cmd, title=f"RETROSPECTIVE SUB-AGENT - {title}")


# ============================================================================
# ENTRY POINT
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Retrospective sub-agent — transcript analysis worker",
    )
    parser.add_argument("--step", type=int, required=True)
    # --transcript / --since are accepted but unused: the launching command
    # carries them so downstream re-invocations can't silently drop them
    # under refactor. PARSE step instructions tell the LLM to read the
    # values from the prose `TRANSCRIPT_PATH=` / `SINCE=` labels.
    parser.add_argument("--transcript", type=str, default=None)
    parser.add_argument("--since", type=str, default="all")
    args = parser.parse_args()

    if args.step < 1 or args.step > TOTAL_STEPS:
        sys.exit(f"ERROR: --step must be 1-{TOTAL_STEPS}")

    print(format_output(args.step))


if __name__ == "__main__":
    main()
