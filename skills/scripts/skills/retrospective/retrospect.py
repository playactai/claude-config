#!/usr/bin/env python3
"""
Retrospective skill — parent orchestrator.

Five-step workflow:
  1. LOCATE   - Find the current session's transcript JSONL (real work)
  2. DISPATCH - Spawn the analysis sub-agent (prompt → LLM uses Task tool)
  3. TRIAGE   - Parse PROPOSALS JSON, dedupe, tag [NEW]/[RECURRING]
  4. APPROVE  - Per-item approval via AskUserQuestion (≤3 batched)
  5. APPLY    - Detect collisions, append ledger, write approved memory edits

The LLM drives steps 2-5 by reading the printed instructions and using its
own tools. Step 1 alone executes Python logic, since transcript discovery
needs to happen before the orchestrator's first prompt is rendered.
"""

import argparse
import os
import shlex
import sys
from pathlib import Path
from textwrap import indent
from typing import Literal

from skills.lib.workflow.core import StepDef, Workflow
from skills.lib.workflow.prompts import format_step, subagent_dispatch
from skills.retrospective import transcript
from skills.retrospective.proposals import EMPTY_DECISIONS_JSON

# Closed enumeration of how `resolve_project_dir` chose its result. Surfaced
# in LOCATE_OK so the user can tell when --session-id won over --cwd.
ResolvedSource = Literal["--project-dir", "--session-id", "--cwd", "auto-detect"]

# ============================================================================
# SHARED PROMPTS
# ============================================================================
#
# Used by 2+ workflow steps. Drift here breaks consumers in lock-step.

# Re-render EMPTY_DECISIONS_JSON with literal `{` / `}` escaped for str.format
# and the `<uuid>` placeholder replaced by the format-time `{session_id}` key.
# The `EMPTY_DECISIONS_DIRECTIVE` below is itself .format()-substituted by
# build_triage_body / build_approve_body, so JSON braces must be escaped.
_ESCAPED_EMPTY_DECISIONS_JSON = (
    EMPTY_DECISIONS_JSON.replace("{", "{{")
    .replace("}", "}}")
    .replace("<uuid>", "{session_id}")
)

# Directive used by step 3's count==0 fallback and step 4's empty-proposals
# fallback. Both paths must satisfy step 5's precondition that the decisions
# JSON file exists with the canonical shape (single source of truth lives
# in proposals.py as EMPTY_DECISIONS_JSON).
EMPTY_DECISIONS_DIRECTIVE = (
    "Use the Write tool to create {decisions_path} with this exact content\n"
    "so step 5 has valid input:\n"
    "  " + _ESCAPED_EMPTY_DECISIONS_JSON + "\n"
    "If the Write fails, STOP and report the error — do not advance to\n"
    "step 5. Otherwise, advance with --no-ledger appended so the ledger is\n"
    "left untouched.\n"
)

# Frontmatter shape rendered by step 5 (live + dry-run variants). Lives in
# SHARED PROMPTS so both apply paths reference the same string and can't
# silently drift out of sync.
MEMORY_FRONTMATTER_SHAPE = (
    "---\n"
    "name: <item.name>\n"
    "description: <item.description>\n"
    "type: <item.memory_type>\n"
    "originSessionId: <session_id>\n"
    "---\n"
    "<item.body>"
)


# ============================================================================
# CONFIGURATION
# ============================================================================

MODULE_PATH = "skills.retrospective.retrospect"
SUBAGENT_MODULE_PATH = "skills.retrospective.subagent"
TOTAL_STEPS = 5

LEDGER_RELATIVE = Path("retrospective") / "observations.jsonl"
MEMORY_RELATIVE = Path("memory")
INDEX_RELATIVE = MEMORY_RELATIVE / "MEMORY.md"

# Per-session staging files. Lives under /tmp because the LLM only needs them
# to pass state across step boundaries; they do not need to survive the
# session. Cleaned at the end of step 5 (live mode).
PAYLOAD_TMPL = "/tmp/retrospective-{session}-payload.json"
TAGGED_TMPL = "/tmp/retrospective-{session}-tagged.json"
VERDICTS_TMPL = "/tmp/retrospective-{session}-verdicts.json"
DECISIONS_TMPL = "/tmp/retrospective-{session}-decisions.json"

# Path the Bash invocations cd into before running the proposals helpers.
SCRIPTS_DIR = Path(__file__).resolve().parents[2]


# ============================================================================
# MESSAGE TEMPLATES
# ============================================================================

# --- STEP 1: LOCATE ---------------------------------------------------------

LOCATE_HEADER = (
    "LOCATE - Resolve the current session's transcript JSONL.\n"
    "\n"
)

LOCATE_NO_PROJECTS_DIR = (
    "RESULT: No ~/.claude/projects/ directory exists.\n"
    "\n"
    "There are no Claude Code transcripts on this machine yet, so there is\n"
    "nothing to retrospect on. Exit without further work.\n"
)

LOCATE_NO_TRANSCRIPTS = (
    "RESULT: No transcript files (*.jsonl) found under any project.\n"
    "\n"
    "Either this is a fresh install or transcripts have been pruned.\n"
    "Nothing to retrospect on. Exit.\n"
)

LOCATE_NO_PROJECT_FOR_CWD = (
    "RESULT: Resolved project dir does not exist on disk.\n"
    "\n"
    "Project dir : {project_dir}\n"
    "Resolved via: {resolved_via}\n"
    "\n"
    "No Claude Code session has been recorded against this directory yet.\n"
    "Re-invoke /retrospective from a project that has a transcript, or\n"
    "pass --session-id <uuid> to point at a specific recorded session.\n"
)

LOCATE_NO_SESSION = (
    "RESULT: Project directory exists but contains no .jsonl files.\n"
    "\n"
    "Path: {project_dir}\n"
    "\n"
    "Re-invoke /retrospective from inside an active Claude Code session\n"
    "(this skill needs the session's transcript on disk).\n"
)

LOCATE_INVALID_SINCE = (
    "RESULT: --since value is invalid.\n"
    "\n"
    "Got:    --since {since}\n"
    "Error:  {error}\n"
    "\n"
    "Valid forms: Nh / Nd / Nw / Nm (m ≈ 30 days), ISO 8601 timestamp, or 'all'.\n"
    "Re-invoke with one of those.\n"
)

LOCATE_INVALID_PROJECT_DIR = (
    "RESULT: --project-dir is not under ~/.claude/projects/.\n"
    "\n"
    "Got:    --project-dir {project_dir}\n"
    "Error:  {error}\n"
    "\n"
    "Refusing to compute derived paths outside the projects root. If you\n"
    "skipped step 1, re-run from --step 1 so step 1 can resolve it.\n"
)

LOCATE_OK = (
    "RESULT: Located active transcript.\n"
    "\n"
    "Project dir : {project_dir}\n"
    "Resolved via: {resolved_via}\n"
    "Transcript  : {transcript_path}\n"
    "Session ID  : {session_id}\n"
    "Lines       : {line_count}\n"
    "Range       : {earliest} → {latest}\n"
    "Window      : --since {since}\n"
    "\n"
    "Forward state: --transcript and --project-dir will be passed to step 2."
)

# --- STEP 2: DISPATCH -------------------------------------------------------

DISPATCH_TASK_PREAMBLE = (
    "You will analyze a Claude Code transcript JSONL and produce a structured\n"
    "PROPOSALS JSON block. The transcript path is supplied below — read ONLY\n"
    "that file. Do not roam to other transcripts.\n"
    "\n"
    "Walk through your own four-step workflow (parse → detect signals →\n"
    "classify → report) by running the invoke command below. Each step prints\n"
    "instructions for the next phase; follow them in order.\n"
    "\n"
    "When you reach the report step, emit a SINGLE fenced JSON block with the\n"
    "PROPOSALS schema. That JSON block is your final output to me. Do not\n"
    "summarize or commentate around it.\n"
    "\n"
    "Memory entry bodies you propose must follow the structured form mandated\n"
    "by the user's global CLAUDE.md (rule first, then **Why:** and **How to\n"
    "apply:** for feedback/project memories). Use the encoded project dir for\n"
    "absolute target paths:\n"
    "  PROJECT_DIR={project_dir}\n"
    "  New entries go in: {project_dir}/memory/<slug>.md\n"
    "  TRANSCRIPT_PATH={transcript_path}\n"
    "  SINCE={since}\n"
)

DISPATCH_AFTER_GUIDANCE = (
    "WAIT for the sub-agent's PROPOSALS JSON block. When it returns, copy the\n"
    "JSON into the orchestrator context for step 3 — do NOT summarize it.\n"
    "\n"
    "Then advance to TRIAGE."
)

# --- STEP 3: TRIAGE ---------------------------------------------------------

TRIAGE_INSTRUCTIONS = (
    "TRIAGE - Parse the sub-agent's PROPOSALS JSON, dedupe, and tag frequency.\n"
    "\n"
    "INPUT: The sub-agent returned a fenced JSON block in your context. It\n"
    "looks like:\n"
    "  {{\"version\":1,\"session_id\":\"...\",\"proposals\":[{{...}}, ...]}}\n"
    "\n"
    "ACTIONS:\n"
    "  1. Save the sub-agent's PROPOSALS JSON verbatim to:\n"
    "       {payload_path}\n"
    "     (Use the Write tool. Do not edit, paraphrase, or summarize the\n"
    "     content — the helper expects the raw block.)\n"
    "\n"
    "  2. Run the tagger via Bash. This dedupes, applies the path-traversal\n"
    "     guard, suppresses already-applied fingerprints, and emits a tagged\n"
    "     JSON to {tagged_path}:\n"
    "\n"
    "{tagger_cmd}\n"
    "\n"
    "     If the tagger exits non-zero, print its stderr and STOP — the\n"
    "     sub-agent's JSON is malformed; do not improvise.\n"
    "\n"
    "  3. Read {tagged_path}. The shape is:\n"
    "       {{\"count\": N, \"suppressed_already_applied\": M,\n"
    "         \"rejected_unsafe\": [...], \"proposals\": [...]}}\n"
    "\n"
    "  4. If count == 0:\n"
    "       Print 'No proposals — session was clean. Nothing to apply.'\n"
    "       Note any rejected_unsafe entries (the sub-agent emitted targets\n"
    "       outside the memory dir; surface them so the user knows).\n"
    + indent(EMPTY_DECISIONS_DIRECTIVE, "       ")
    + "\n"
      "  5. Otherwise, PRINT a numbered list with one line per proposal:\n"
      "       N. [<frequency>] <kind>  <target>\n"
      "          rationale: <one line>\n"
      "          evidence: <count> citations\n"
      "\n"
      "ADVANCE: invoke the next-step command shown below. Step 4 will read the\n"
      "tagged file from {tagged_path} (path is derived from session-id, no\n"
      "extra flag needed)."
)

# --- STEP 4: APPROVE --------------------------------------------------------

APPROVE_INSTRUCTIONS = (
    "APPROVE - Walk the user through per-item approval.\n"
    "\n"
    "INPUT: read the tagged proposals from:\n"
    "    {tagged_path}\n"
    "(this path is derived from session-id; the script knows it too).\n"
    "If proposals is empty:\n"
    + indent(EMPTY_DECISIONS_DIRECTIVE, "  ")
    + "\n"
      "BATCH POLICY:\n"
    "  AskUserQuestion accepts at most 3 questions per call and at most 3\n"
    "  explicit options per question (the tool always adds an implicit\n"
    "  'Other' free-text option, which we lean on for the edit flow below).\n"
    "  - 1 to 3 proposals: ONE AskUserQuestion call with one question per\n"
    "    proposal. Each question:\n"
    "      header: 'Proposal N' (max 12 chars)\n"
    "      question: '<frequency-tag> <kind>: <name>?'\n"
    "        Include the proposal body / diff in the question text so the\n"
    "        user can read it without expanding anything. If a body exceeds\n"
    "        ~800 characters, print the full body before the\n"
    "        AskUserQuestion call and reference 'see body above' in the\n"
    "        question text — long question text may exceed the tool's cap.\n"
    "      options:\n"
    "        - apply (Recommended)\n"
    "        - reject\n"
    "        - defer\n"
    "\n"
    "  - 4+ proposals: print the full numbered list with body previews,\n"
    "    then call AskUserQuestion in batches of 3 (same option set).\n"
    "\n"
    "OPTION SEMANTICS:\n"
    "  apply  - write this memory entry in step 5; ledger status = applied.\n"
    "  reject - do not write; ledger status = rejected.\n"
    "  defer  - do not write this run; ledger status = deferred. Will\n"
    "           resurface tagged [RECURRING] next run.\n"
    "  Other  - the user can always select the implicit 'Other' option to\n"
    "           supply free text. Treat substantive 'Other' text as a body\n"
    "           edit and run the inline edit sub-flow:\n"
    "             1. Update the proposal's body in your in-memory copy with\n"
    "                the user-supplied text.\n"
    "             2. Re-confirm via a fresh single-question AskUserQuestion\n"
    "                (apply / reject) showing the edited body.\n"
    "             3. The edited body flows into the verdicts file's\n"
    "                edited_body field; the fingerprint stays the same.\n"
    "           If the 'Other' text is unclear or not a body, ask the user\n"
    "           via a plain-text message what they want to do.\n"
    "\n"
    "WRITE the verdicts JSON to {verdicts_path} as a JSON list. One entry per\n"
    "proposal, in any order:\n"
    "    [\n"
    "      {{\"id\": \"p1\", \"verdict\": \"apply\", \"edited_body\": null}},\n"
    "      {{\"id\": \"p2\", \"verdict\": \"reject\"}},\n"
    "      {{\"id\": \"p3\", \"verdict\": \"defer\"}}\n"
    "    ]\n"
    "Where verdict is one of \"apply\" | \"reject\" | \"defer\". Set\n"
    "edited_body (apply only) to the user-supplied body if Other-edit was\n"
    "taken; otherwise omit or leave null.\n"
    "\n"
    "COMPOSE the decisions file via the helper. This joins the tagged file\n"
    "with the verdicts and writes the canonical decisions JSON for step 5\n"
    "(fingerprints carried through; no LLM hand-authoring of structured fields):\n"
    "\n"
    "{compose_cmd}\n"
    "\n"
    "If the helper exits non-zero, print its stderr and STOP — the verdicts\n"
    "file is malformed; do not improvise.\n"
    "\n"
    "ADVANCE: invoke step 5 once decisions are composed."
)

# --- STEP 5: APPLY ----------------------------------------------------------
#
# Order matters: collision detection and ledger append happen BEFORE the
# per-item write loop. An interrupted write loop leaves a consistent ledger
# (the user's decisions are recorded), and collisions have been flipped to
# deferred so the ledger reflects the on-disk truth.

APPLY_INSTRUCTIONS = (
    "APPLY - Write approved memory edits and record the ledger.\n"
    "\n"
    "INPUT: decisions JSON at __DECISIONS_PATH__ (path derived from session-id).\n"
    "The file has 'applied' (drives memory writes) and 'entries' (drives\n"
    "ledger append).\n"
    "\n"
    "ORDER MATTERS: collision detection and ledger append happen BEFORE the\n"
    "per-item write loop. An interrupted run leaves a consistent ledger.\n"
    "\n"
    "ACTIONS:\n"
    "  1. Ensure the memory dir exists:\n"
    "\n"
    "__MKDIR_CMD__\n"
    "\n"
    "  2. Pre-flight collision check. For each item in `applied` whose kind\n"
    "     is \"memory:create\", run via Bash: `test -e <item.target>`. Collect\n"
    "     the IDs of items whose target already exists into a comma-separated\n"
    "     string (call it COLLIDING_IDS). If none collide, COLLIDING_IDS is\n"
    "     empty.\n"
    "\n"
    "  3. If COLLIDING_IDS is non-empty, flip them in the decisions file via\n"
    "     the helper (drops them from `applied`, sets their `entries` row to\n"
    "     deferred so the proposals resurface [RECURRING] next run):\n"
    "\n"
    "__MARK_COLLISIONS_TEMPLATE__\n"
    "\n"
    "     Replace <COMMA_SEPARATED_IDS> with COLLIDING_IDS. Skip if empty.\n"
    "\n"
    "  4. Append the ledger from the (now-correctly-statused) decisions file.\n"
    "     Skip this command if --no-ledger was passed:\n"
    "\n"
    "__APPEND_LEDGER_CMD__\n"
    "\n"
    "  5. RE-READ __DECISIONS_PATH__ now that collisions have been flipped,\n"
    "     and walk the surviving `applied` items. For each:\n"
    "       a. memory:create — Use the Write tool to create item.target with:\n"
    + indent(MEMORY_FRONTMATTER_SHAPE, "            ")
    + "\n"
      "       b. memory:update — Use the Edit tool with item.body as the\n"
      "          REPLACEMENT body. If the target does not exist (Edit errors\n"
      "          with 'File does not exist'), fall back to the Write tool\n"
      "          with the same frontmatter shape as memory:create. Otherwise,\n"
      "          preserve existing frontmatter unless item.name or\n"
      "          item.description differ.\n"
      "       c. memory:delete — Use Bash 'rm -f' for item.target.\n"
      "\n"
      "  6. Update the index idempotently. Partition the surviving applied\n"
      "     items by kind:\n"
      "       - For each item with kind 'memory:create' or 'memory:update',\n"
      "         add one --entry '<name>|<basename.md>|<description>'\n"
      "         (basename is the last path segment of item.target).\n"
      "       - For each item with kind 'memory:delete', add one --delete\n"
      "         '<basename.md>'.\n"
      "     Use the template below as a shape reference — DROP any line\n"
      "     whose kind has zero items (and remove the dangling '\\' from\n"
      "     the new last argument). The helper rejects a basename\n"
      "     appearing in both --entry and --delete; that conflict means\n"
      "     two proposals collided on the same basename — STOP and\n"
      "     report it instead of retrying.\n"
      "\n"
      "__UPDATE_INDEX_TEMPLATE__\n"
      "\n"
      "  7. Cleanup the per-session staging files:\n"
      "\n"
      "__CLEANUP_CMD__\n"
      "\n"
      "  8. Print a final summary:\n"
      "       APPLIED:    <list of paths actually written>\n"
      "       COLLISIONS: <list of memory:create paths flipped to deferred>\n"
      "       REJECTED:   <count of entries with status=='rejected'>\n"
      "       DEFERRED:   <U + C> = <U> user-deferred + <C> auto-deferred from collisions\n"
      "       LEDGER:     __LEDGER_PATH__ (N new lines)\n"
      "\n"
      "WORKFLOW COMPLETE - return the summary as your final response."
)


# --- STEP 5: APPLY (DRY-RUN VARIANT) ----------------------------------------
#
# When --dry-run is set the contract is "no writes anywhere", which means
# the apply prompt must NOT instruct the LLM to call Write/Edit/rm or the
# ledger helper. Steering by a different prompt body is more reliable than
# trying to encode "skip these branches" inside one prompt. /tmp staging
# files persist after dry-run; the next live invocation cleans them.

APPLY_DRY_RUN_INSTRUCTIONS = (
    "APPLY (DRY RUN) - Preview approved edits without writing anything.\n"
    "\n"
    "--dry-run was passed. The contract is: NO file writes anywhere on disk.\n"
    "Do NOT call Write, Edit, NotebookEdit, or any Bash command that mutates\n"
    "the filesystem (no mkdir, rm, touch, cp, redirect-to-file). The ledger\n"
    "helper is also skipped.\n"
    "\n"
    "INPUT: read decisions from:\n"
    "    __DECISIONS_PATH__\n"
    "\n"
    "ACTIONS:\n"
    "  1. For each item in 'applied', PRINT a fenced preview block showing:\n"
    "       --- <item.target> (<item.kind>) ---\n"
    + indent(MEMORY_FRONTMATTER_SHAPE, "       ")
    + "\n"
      "     For memory:create items, also run `test -e <item.target>` via Bash\n"
      "     (read-only, allowed in dry-run). If it exists, mark the preview as\n"
      "     'COLLISION — would be skipped in live apply' so the user sees what\n"
      "     would happen.\n"
      "\n"
      "  2. PRINT the index update that WOULD be appended to:\n"
      "       __INDEX_PATH__\n"
      "     One line per applied entry:\n"
      "       - [<name>](<basename>.md) — <description>\n"
      "\n"
      "  3. PRINT the ledger entries that WOULD be appended:\n"
      "       __LEDGER_PATH__\n"
      "     One JSON line per entry from the decisions 'entries' array.\n"
      "\n"
      "  4. Print a final summary noting that nothing was written:\n"
      "       DRY RUN — no files modified. Re-invoke without --dry-run to apply.\n"
      "       WOULD APPLY:  <count>\n"
      "       WOULD REJECT: <count>\n"
      "       WOULD DEFER:  <count>\n"
      "       (per-session staging files at /tmp/retrospective-{session}-* persist;\n"
      "        the next live invocation cleans them.)\n"
      "\n"
      "WORKFLOW COMPLETE - return the summary as your final response."
)


# ============================================================================
# DOMAIN LOGIC
# ============================================================================
#
# Resolution + validation helpers used by the message builders below.
# Defined first so the builders can call them without forward references.


def resolve_user_cwd(args: argparse.Namespace) -> Path | None:
    """Determine the user's working directory for transcript lookup.

    Precedence: --cwd > CLAUDE_PROJECT_DIR > None.
    Returns None if the script must fall back to active-project autodetection.
    """
    if args.cwd:
        return Path(args.cwd).expanduser()
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env).expanduser()
    return None


def resolve_project_dir(args: argparse.Namespace) -> tuple[Path | None, ResolvedSource]:
    """Resolve the project transcript directory under ~/.claude/projects/.

    Returns (path, source) where source is a `ResolvedSource` literal:
      "--project-dir"     - explicit caller-provided path (re-invocation).
      "--session-id"      - cross-project UUID lookup hit.
      "--cwd"             - encoded from --cwd / CLAUDE_PROJECT_DIR.
      "auto-detect"       - most-recently-modified transcript on disk.

    Strategy:
      1. If --project-dir given, use it directly.
      2. If --session-id given AND its UUID exists in some project's JSONL,
         use that project — even if --cwd is also set. Honoring the more-
         specific signal lets `--session-id <uuid>` work from anywhere.
         An empty/whitespace-only --session-id is treated like None to
         match the help text ("Override transcript-by-mtime selection").
      3. If user cwd is known (--cwd or CLAUDE_PROJECT_DIR), encode it.
      4. Otherwise auto-detect by most-recently-modified JSONL.

    Path is None when nothing on disk matches the auto-detect fallback.
    """
    if args.project_dir:
        return Path(args.project_dir).expanduser(), "--project-dir"
    if args.session_id and args.session_id.strip():
        hit = transcript.find_session_across_projects(args.session_id)
        if hit is not None:
            return hit, "--session-id"
    cwd = resolve_user_cwd(args)
    if cwd is not None:
        return transcript.get_project_dir(cwd), "--cwd"
    return transcript.find_active_project_dir(), "auto-detect"


def validate_project_dir(project_dir: str | None) -> tuple[Path | None, str | None]:
    """Confirm project_dir resolves to a path under ~/.claude/projects/.

    Refuses paths outside the projects root so the apply step can't compute
    a derived path that escapes (e.g., --project-dir /etc → /etc/memory/...).

    Returns (resolved_path, error). If project_dir is None/empty, returns
    (None, None) so the caller falls back to placeholder substitution
    (registry sanity test mode).
    """
    if not project_dir:
        return None, None
    try:
        resolved = Path(project_dir).expanduser().resolve()
    except (OSError, ValueError) as e:
        return None, f"could not resolve {project_dir!r}: {e}"
    projects_root = transcript.DEFAULT_PROJECTS_ROOT.resolve()
    if not resolved.is_relative_to(projects_root):
        return None, f"{project_dir!r} (resolved: {resolved}) is not under {projects_root}"
    return resolved, None


# ============================================================================
# MESSAGE BUILDERS
# ============================================================================
#
# Helpers ordered top-to-bottom so dependencies are defined before use.


def _str_or_placeholder(value: str | None, placeholder: str) -> str:
    """Return value if non-empty, else the literal placeholder.

    Steps 2-5 emit prompts that interpolate forward-state args. The registry
    sanity test invokes them with only --step, so we substitute literal
    placeholder strings (rendered verbatim) instead of crashing. A real
    invocation that hits these placeholders means the user skipped step 1.
    """
    return value if value else placeholder


def _forward_state_args(args: argparse.Namespace, *extra: str) -> str:
    """Build the trailing CLI arg list shared across all step transitions.

    Returns a single space-joined string so callers can append it to a
    `cd ... && uv run python -m ... --step N` prefix.
    """
    parts: list[str] = []
    if args.transcript:
        parts.append(f"--transcript {shlex.quote(args.transcript)}")
    if args.project_dir:
        parts.append(f"--project-dir {shlex.quote(args.project_dir)}")
    if args.session_id:
        parts.append(f"--session-id {shlex.quote(args.session_id)}")
    if args.since:
        parts.append(f"--since {shlex.quote(args.since)}")
    if args.dry_run:
        parts.append("--dry-run")
    if args.no_ledger:
        parts.append("--no-ledger")
    parts.extend(extra)
    return " ".join(parts)


def _session_paths(session_id: str | None) -> tuple[str, str, str, str]:
    """Resolve (payload, tagged, verdicts, decisions) tmp paths for a session id."""
    sid = session_id or "<SESSION_ID>"
    return (
        PAYLOAD_TMPL.format(session=sid),
        TAGGED_TMPL.format(session=sid),
        VERDICTS_TMPL.format(session=sid),
        DECISIONS_TMPL.format(session=sid),
    )


def _bash_command(lines: list[str]) -> str:
    """Render a multi-line bash command as a single indented block.

    Each line is prefixed with 7 spaces to align under "       " in the
    surrounding numbered-list prompt.
    """
    return "\n".join(f"       {line}" for line in lines)


def _build_tagger_cmd(
    skills_dir: str, payload: str, ledger: str, project_dir: str, tagged: str
) -> str:
    """Compose the `proposals tag` invocation with every path shlex-quoted."""
    return _bash_command(
        [
            f"cd {shlex.quote(skills_dir)} && uv run python -m skills.retrospective.proposals tag \\",
            f"  --payload {shlex.quote(payload)} \\",
            f"  --ledger {shlex.quote(ledger)} \\",
            f"  --project-dir {shlex.quote(project_dir)} \\",
            f"  --output {shlex.quote(tagged)}",
        ]
    )


def _build_compose_decisions_cmd(
    skills_dir: str, tagged: str, verdicts: str, session_id: str, decisions: str
) -> str:
    """Compose the `proposals compose-decisions` invocation."""
    return _bash_command(
        [
            f"cd {shlex.quote(skills_dir)} && uv run python -m skills.retrospective.proposals compose-decisions \\",
            f"  --tagged {shlex.quote(tagged)} \\",
            f"  --verdicts {shlex.quote(verdicts)} \\",
            f"  --session-id {shlex.quote(session_id)} \\",
            f"  --output {shlex.quote(decisions)}",
        ]
    )


def _build_mark_collisions_template(skills_dir: str, decisions: str) -> str:
    """Compose the `proposals mark-collisions` invocation.

    `<COMMA_SEPARATED_IDS>` is a literal placeholder the LLM substitutes at
    runtime with the colliding-id list it gathered in step-5 action 2.
    """
    return _bash_command(
        [
            f"cd {shlex.quote(skills_dir)} && uv run python -m skills.retrospective.proposals mark-collisions \\",
            f"  --decisions {shlex.quote(decisions)} \\",
            "  --collisions <COMMA_SEPARATED_IDS>",
        ]
    )


def _build_append_ledger_cmd(skills_dir: str, decisions: str, ledger: str) -> str:
    return _bash_command(
        [
            f"cd {shlex.quote(skills_dir)} && uv run python -m skills.retrospective.proposals append-ledger \\",
            f"  --decisions {shlex.quote(decisions)} \\",
            f"  --ledger {shlex.quote(ledger)}",
        ]
    )


def _build_update_index_template(skills_dir: str, index_path: str) -> str:
    """Compose `proposals update-index` template. At runtime the LLM
    substitutes one --entry flag per memory:create / memory:update item
    and one --delete flag per memory:delete item; the template body shows
    one of each as a worked example. Per-kind partition guidance lives
    in the surrounding step-6 prose, not as an inline shell comment
    (which an LLM might paste verbatim)."""
    return _bash_command(
        [
            f"cd {shlex.quote(skills_dir)} && uv run python -m skills.retrospective.proposals update-index \\",
            f"  --index {shlex.quote(index_path)} \\",
            "  --entry '<name>|<basename.md>|<description>' \\",
            "  --delete '<basename.md>'",
        ]
    )


def _build_mkdir_cmd(memory_dir: str) -> str:
    return _bash_command([f"mkdir -p {shlex.quote(memory_dir)}"])


def _build_cleanup_cmd(payload: str, tagged: str, verdicts: str, decisions: str) -> str:
    return _bash_command(
        [
            f"rm -f {shlex.quote(payload)} {shlex.quote(tagged)} \\",
            f"      {shlex.quote(verdicts)} {shlex.quote(decisions)}",
        ]
    )


def build_locate_body(args: argparse.Namespace) -> tuple[str, str | None]:
    """Run step-1 logic and return (body_text, next_cmd).

    next_cmd is None when the workflow should terminate (no transcript / bad
    --since / scope-leak attempt).
    """
    if not transcript.DEFAULT_PROJECTS_ROOT.exists():
        return LOCATE_HEADER + LOCATE_NO_PROJECTS_DIR, None

    # If the user passed --project-dir explicitly, refuse anything outside
    # ~/.claude/projects/ before doing any work. Catches `--project-dir /etc`
    # at the earliest possible step instead of letting the sub-agent be
    # dispatched against a bad scope and surfacing it later as rejected_unsafe.
    if args.project_dir:
        _resolved, err = validate_project_dir(args.project_dir)
        if err:
            return (
                LOCATE_HEADER + LOCATE_INVALID_PROJECT_DIR.format(
                    project_dir=args.project_dir, error=err
                ),
                None,
            )

    project_dir, resolved_via = resolve_project_dir(args)
    if project_dir is None:
        return LOCATE_HEADER + LOCATE_NO_TRANSCRIPTS, None
    if not project_dir.exists():
        return (
            LOCATE_HEADER + LOCATE_NO_PROJECT_FOR_CWD.format(
                project_dir=project_dir, resolved_via=resolved_via
            ),
            None,
        )

    session_path = transcript.find_current_session(project_dir, session_id=args.session_id)
    if session_path is None:
        return (
            LOCATE_HEADER + LOCATE_NO_SESSION.format(project_dir=project_dir),
            None,
        )

    try:
        since_dt = transcript.parse_since(args.since)
    except ValueError as e:
        return (
            LOCATE_HEADER + LOCATE_INVALID_SINCE.format(since=args.since, error=str(e)),
            None,
        )
    line_count, earliest, latest = transcript.transcript_summary(session_path, since=since_dt)
    session_id = session_path.stem

    body = LOCATE_HEADER + LOCATE_OK.format(
        project_dir=project_dir,
        transcript_path=session_path,
        session_id=session_id,
        line_count=line_count,
        earliest=earliest or "(no timestamped messages)",
        latest=latest or "(no timestamped messages)",
        since=args.since,
        resolved_via=resolved_via,
    )

    # Hydrate args with the discovered values so _forward_state_args picks
    # them up; this keeps the next-command construction in one place.
    args.transcript = str(session_path)
    args.project_dir = str(project_dir)
    args.session_id = session_id
    next_cmd = f"uv run python -m {MODULE_PATH} --step 2 {_forward_state_args(args)}"
    return body, next_cmd


def build_dispatch_body(args: argparse.Namespace) -> str:
    """Compose step-2 body: subagent dispatch block + post-instructions."""
    _resolved, err = validate_project_dir(args.project_dir)
    if err:
        return LOCATE_HEADER + LOCATE_INVALID_PROJECT_DIR.format(
            project_dir=args.project_dir, error=err
        )
    transcript_path = _str_or_placeholder(args.transcript, "<TRANSCRIPT_PATH>")
    project_dir = _str_or_placeholder(args.project_dir, "<PROJECT_DIR>")
    since = args.since or "all"
    invoke_cmd = (
        f"uv run python -m {SUBAGENT_MODULE_PATH} --step 1"
        f" --transcript {shlex.quote(transcript_path)}"
        f" --since {shlex.quote(since)}"
    )
    task_section = DISPATCH_TASK_PREAMBLE.format(
        project_dir=project_dir,
        transcript_path=transcript_path,
        since=since,
    )
    dispatch_block = subagent_dispatch(
        agent_type="general-purpose",
        command=invoke_cmd,
        prompt=task_section,
        model="sonnet",
    )
    return dispatch_block + "\n\n" + DISPATCH_AFTER_GUIDANCE


def build_triage_body(args: argparse.Namespace) -> str:
    resolved, err = validate_project_dir(args.project_dir)
    if err:
        return LOCATE_HEADER + LOCATE_INVALID_PROJECT_DIR.format(
            project_dir=args.project_dir, error=err
        )
    project_dir = (
        str(resolved) if resolved is not None
        else _str_or_placeholder(args.project_dir, "<PROJECT_DIR>")
    )
    ledger_path = str(Path(project_dir) / LEDGER_RELATIVE)
    payload_path, tagged_path, _verdicts_path, decisions_path = _session_paths(args.session_id)
    session_id = _str_or_placeholder(args.session_id, "<SESSION_ID>")
    tagger_cmd = _build_tagger_cmd(
        skills_dir=str(SCRIPTS_DIR),
        payload=payload_path,
        ledger=ledger_path,
        project_dir=project_dir,
        tagged=tagged_path,
    )
    return TRIAGE_INSTRUCTIONS.format(
        payload_path=payload_path,
        tagged_path=tagged_path,
        tagger_cmd=tagger_cmd,
        decisions_path=decisions_path,
        session_id=session_id,
    )


def build_approve_body(args: argparse.Namespace) -> str:
    _, tagged_path, verdicts_path, decisions_path = _session_paths(args.session_id)
    session_id = _str_or_placeholder(args.session_id, "<SESSION_ID>")
    compose_cmd = _build_compose_decisions_cmd(
        skills_dir=str(SCRIPTS_DIR),
        tagged=tagged_path,
        verdicts=verdicts_path,
        session_id=session_id,
        decisions=decisions_path,
    )
    return APPROVE_INSTRUCTIONS.format(
        tagged_path=tagged_path,
        verdicts_path=verdicts_path,
        decisions_path=decisions_path,
        session_id=session_id,
        compose_cmd=compose_cmd,
    )


def build_apply_body(args: argparse.Namespace) -> str:
    resolved, err = validate_project_dir(args.project_dir)
    if err:
        return LOCATE_HEADER + LOCATE_INVALID_PROJECT_DIR.format(
            project_dir=args.project_dir, error=err
        )
    project_dir = (
        str(resolved) if resolved is not None
        else _str_or_placeholder(args.project_dir, "<PROJECT_DIR>")
    )
    project_path = Path(project_dir)
    memory_dir = str(project_path / MEMORY_RELATIVE)
    index_path = str(project_path / INDEX_RELATIVE)
    ledger_path = str(project_path / LEDGER_RELATIVE)
    payload_path, tagged_path, verdicts_path, decisions_path = _session_paths(args.session_id)

    template = APPLY_DRY_RUN_INSTRUCTIONS if args.dry_run else APPLY_INSTRUCTIONS

    body = (
        template
        .replace("__PROJECT_DIR__", project_dir)
        .replace("__INDEX_PATH__", index_path)
        .replace("__LEDGER_PATH__", ledger_path)
        .replace("__DECISIONS_PATH__", decisions_path)
    )
    if not args.dry_run:
        body = (
            body
            .replace("__MKDIR_CMD__", _build_mkdir_cmd(memory_dir))
            .replace(
                "__MARK_COLLISIONS_TEMPLATE__",
                _build_mark_collisions_template(str(SCRIPTS_DIR), decisions_path),
            )
            .replace(
                "__APPEND_LEDGER_CMD__",
                _build_append_ledger_cmd(
                    skills_dir=str(SCRIPTS_DIR),
                    decisions=decisions_path,
                    ledger=ledger_path,
                ),
            )
            .replace(
                "__UPDATE_INDEX_TEMPLATE__",
                _build_update_index_template(str(SCRIPTS_DIR), index_path),
            )
            .replace(
                "__CLEANUP_CMD__",
                _build_cleanup_cmd(payload_path, tagged_path, verdicts_path, decisions_path),
            )
        )
    return body


def build_next_command(step: int, args: argparse.Namespace) -> str | None:
    """Build the invoke command for the step *after* the given step.

    Step 1's next_cmd is computed inside build_locate_body (it depends on
    discovered state). This helper handles steps 2-4.
    """
    if step >= TOTAL_STEPS:
        return None
    base = f"uv run python -m {MODULE_PATH} --step {step + 1}"
    forwarded = _forward_state_args(args)
    return f"{base} {forwarded}".rstrip()


# ============================================================================
# STEP DEFINITIONS
# ============================================================================

WORKFLOW = Workflow(
    "retrospective",
    StepDef(id="locate", title="Locate Transcript", actions=[LOCATE_HEADER]),
    StepDef(id="dispatch", title="Dispatch Sub-agent", actions=[DISPATCH_TASK_PREAMBLE]),
    StepDef(id="triage", title="Triage", actions=[TRIAGE_INSTRUCTIONS]),
    StepDef(id="approve", title="Approve", actions=[APPROVE_INSTRUCTIONS]),
    StepDef(id="apply", title="Apply", actions=[APPLY_INSTRUCTIONS]),
    description="Self-improving retrospective: analyze the current session, propose memory edits, and apply approved changes.",
)


# ============================================================================
# OUTPUT FORMATTING
# ============================================================================
#
# Dispatch via DYNAMIC_STEPS dict. Every step depends on `args`, so all are
# dynamic. The third tuple element of each handler is a next-command override:
#   None  -> defer to build_next_command (continue to next step)
#   ""    -> terminate (step 1 error/no-transcript paths, step 5 final, or
#            any step that surfaces a fatal validation error in its body)
#   <str> -> use this verbatim (step 1 success path)

# Sentinel substring identifying a body that is itself a fatal-error report
# (e.g., LOCATE_INVALID_PROJECT_DIR rendered from steps 2-5). When present,
# format_output forces the next-command to "" so the LLM does not advance
# into a step that would re-render the same error.
_FATAL_BODY_MARKERS = (
    "RESULT: --project-dir is not under",
    "RESULT: --since value is invalid",
    "RESULT: No ~/.claude/projects/ directory exists",
    "RESULT: No transcript files",
    "RESULT: Project directory exists but contains no .jsonl files",
)


def _is_fatal_body(body: str) -> bool:
    return any(m in body for m in _FATAL_BODY_MARKERS)


def _format_step_1(args: argparse.Namespace) -> tuple[str, str, str]:
    body, next_cmd = build_locate_body(args)
    return ("Locate", body, next_cmd if next_cmd is not None else "")


def _format_step_2(args: argparse.Namespace) -> tuple[str, str, str | None]:
    return ("Dispatch Sub-agent", build_dispatch_body(args), None)


def _format_step_3(args: argparse.Namespace) -> tuple[str, str, str | None]:
    return ("Triage", build_triage_body(args), None)


def _format_step_4(args: argparse.Namespace) -> tuple[str, str, str | None]:
    return ("Approve", build_approve_body(args), None)


def _format_step_5(args: argparse.Namespace) -> tuple[str, str, str]:
    return ("Apply", build_apply_body(args), "")


DYNAMIC_STEPS = {
    1: _format_step_1,
    2: _format_step_2,
    3: _format_step_3,
    4: _format_step_4,
    5: _format_step_5,
}

_PLACEHOLDER_TOKENS = ("<PROJECT_DIR>", "<TRANSCRIPT_PATH>", "<SESSION_ID>")

_PLACEHOLDER_WARNING = (
    "WARNING: This prompt contains <PROJECT_DIR> / <TRANSCRIPT_PATH> /\n"
    "<SESSION_ID> placeholders. Step 1 has not been run; later steps need\n"
    "the state it sets up. Re-invoke `--step 1` first, then follow its\n"
    "NEXT STEP command. The remaining text is shown for debugging only;\n"
    "do not act on it.\n\n----\n\n"
)


def format_output(args: argparse.Namespace) -> str:
    handler = DYNAMIC_STEPS.get(args.step)
    if handler is None:
        return f"ERROR: Invalid step {args.step}"
    title, body, next_cmd_override = handler(args)

    # If the body is itself a fatal-error report, force termination so the
    # LLM doesn't follow a NEXT STEP into a step that re-renders the same
    # error with forwarded bad state.
    if _is_fatal_body(body):
        next_cmd = ""
    elif next_cmd_override is None:
        next_cmd = build_next_command(args.step, args) or ""
    else:
        next_cmd = next_cmd_override

    # Placeholder leak guard: steps 2-5 require state from step 1. If the
    # rendered body contains any placeholder token, the user invoked us
    # without step-1 state — prepend a loud warning so the LLM doesn't
    # follow placeholder-laced instructions in earnest.
    if args.step >= 2 and any(tok in body for tok in _PLACEHOLDER_TOKENS):
        body = _PLACEHOLDER_WARNING + body

    return format_step(body, next_cmd, title=f"RETROSPECTIVE - {title}")


# ============================================================================
# ENTRY POINT
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Retrospective skill — analyze the current Claude Code session "
        "and propose memory edits the user reviews and approves.",
        epilog="Steps: locate (1) → dispatch (2) → triage (3) → approve (4) → apply (5)",
    )
    parser.add_argument("--step", type=int, required=True)
    parser.add_argument("--cwd", type=str, default=None,
                        help="User's project working directory (overrides $CLAUDE_PROJECT_DIR)")
    parser.add_argument("--session-id", type=str, default=None,
                        help="Override transcript-by-mtime selection with an explicit session UUID")
    parser.add_argument("--since", type=str, default="all",
                        help='Time window: "Nh" / "Nd" / "Nw" / "Nm" / ISO timestamp / "all" (default)')
    parser.add_argument("--transcript", type=str, default=None,
                        help="Resolved transcript path (set by step 1 for downstream steps)")
    parser.add_argument("--project-dir", type=str, default=None,
                        help="Resolved project dir (set by step 1 for downstream steps)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Step 5 prints diffs without writing; implies --no-ledger")
    parser.add_argument("--no-ledger", action="store_true",
                        help="Skip observations.jsonl read/write (test/debug escape hatch)")
    args = parser.parse_args()

    if args.dry_run:
        args.no_ledger = True

    if args.step < 1 or args.step > TOTAL_STEPS:
        sys.exit(f"ERROR: --step must be 1-{TOTAL_STEPS}")

    print(format_output(args))


if __name__ == "__main__":
    main()
