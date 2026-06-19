"""CLI for atomic QR state mutation with file locking.

Usage: uv run python -m skills.planner.cli.qr --state-dir <dir> --qr-phase <phase> <command> [args]

Commands:
  update-item <id> --status <PASS|FAIL> [--finding <text>] [--severity <MUST|SHOULD|COULD>]

Parallel verify agents write to the same qr-{phase}.json file simultaneously.
Without coordination, race conditions corrupt the file:
  Agent A reads {items: [todo, todo]}
  Agent B reads {items: [todo, todo]}
  Agent A writes {items: [pass, todo]}  <- lost update
  Agent B writes {items: [todo, pass]}

This CLI serializes access via file locking and prevents corruption.

This works by:
1. flock(LOCK_EX) on a stable sentinel file (qr-{phase}.lock) that is never
   renamed -- this is what provides real mutual exclusion
2. Read qr-{phase}.json inside the critical section
3. Mutate single item in memory
4. Write via skills.lib.io.atomic_write_text (unique tempfile + os.replace)
5. Release the lock on context exit

WHY a sentinel and not the data file: the data file is replaced via rename on
every write, so a writer that blocks on flock() of the data file wakes holding
a lock on the orphaned pre-rename inode and clobbers the writer that won the
race. The sentinel inode is never renamed, so all writers serialize on one lock.

Invariants:
- Lock holder is sole writer; readers may see stale data but never partial writes
- os.replace() is atomic on POSIX; no reader sees half-written JSON
- PASS is terminal; PASS->FAIL transition errors immediately
- FAIL requires --finding; prevents silent status changes
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import NoReturn
from xml.sax.saxutils import escape

from skills.planner.shared.qr.utils import load_qr_state, qr_write_lock
from skills.planner.shared.schema import canonicalize_severity

from . import qr_commands
from .dispatch import batch as batch_dispatch
from .dispatch import discover_methods, list_methods
from .output import EntityResult, print_entity_result
from .qr_common import (
    assign_group_in_state,
    filtered_items_view,
    find_item,
    is_valid_group_id,
    load_qr_state_under_lock,
    save_qr_state_atomic,
    status_counts,
    update_item_in_state,
)


def error_exit(msg: str, code: int = 1) -> NoReturn:
    """Print error in XML format and exit."""
    print(f"""<qr_cli_error>
  <message>{escape(msg)}</message>
</qr_cli_error>""")
    sys.exit(code)


def get_qr_path(state_dir: str, phase: str) -> Path:
    """Get path to qr-{phase}.json file."""
    return Path(state_dir) / f"qr-{phase}.json"


def _load_qr_state_or_exit(state_dir: str, phase: str) -> dict:
    """Load + validate a QR state file for read-only commands, or error_exit.

    Shared by get-item / list-items / summary. Mutating commands take the lock
    path (load_qr_state_under_lock), not this.
    """
    qr_path = get_qr_path(state_dir, phase)
    if not qr_path.exists():
        error_exit(f"QR state file not found: {qr_path}")
    qr_state = load_qr_state(state_dir, phase)
    if qr_state is None:
        error_exit(f"{qr_path.name} is not a valid QR state object")
    return qr_state


def cmd_update_item(state_dir: str, phase: str, args: list[str]):
    """Update a single QR item status.

    This is the core operation for parallel verify agents.
    Uses file locking to prevent concurrent write corruption.
    """
    if not args:
        error_exit(
            "Usage: update-item <id> --status <PASS|FAIL> [--finding <text>] [--severity <MUST|SHOULD|COULD>]"
        )

    item_id = args[0]
    status = None
    finding = None
    severity = None

    i = 1
    while i < len(args):
        if args[i] == "--status" and i + 1 < len(args):
            status = args[i + 1].upper()
            i += 2
        elif args[i] == "--finding" and i + 1 < len(args):
            finding = args[i + 1]
            i += 2
        elif args[i] == "--severity" and i + 1 < len(args):
            raw = args[i + 1]
            canonical = canonicalize_severity(raw)
            if canonical is None:
                error_exit(
                    f"Invalid severity: {raw}. Must be MUST, SHOULD, or COULD "
                    "(or BLOCKER/CRITICAL)."
                )
            severity = canonical
            i += 2
        else:
            i += 1

    # Validate status
    if not status:
        error_exit("--status required (PASS or FAIL)")

    qr_path = get_qr_path(state_dir, phase)
    if not qr_path.exists():
        error_exit(f"QR state file not found: {qr_path}")

    # Serialize concurrent verify agents on a stable sentinel lock, then run the
    # read -> mutate -> atomic-write cycle. See module docstring for why the data
    # file itself cannot be the lock target.
    with qr_write_lock(state_dir, phase):
        qr_state = load_qr_state_under_lock(qr_path)

        try:
            item = update_item_in_state(qr_state, item_id, status, finding, severity)
        except ValueError as e:
            error_exit(str(e))

        save_qr_state_atomic(qr_path, qr_state)

    # Structured output matching plan.py format
    print_entity_result(EntityResult(id=item_id, version=item["version"], operation="updated"))


def cmd_get_item(state_dir: str, phase: str, args: list[str]):
    """Get a single QR item by ID. For debugging/inspection."""
    if not args:
        error_exit("Usage: get-item <id>")

    item_id = args[0]
    qr_state = _load_qr_state_or_exit(state_dir, phase)

    _, item = find_item(qr_state, item_id)
    if item is None:
        error_exit(f"Item {item_id} not found")

    print(json.dumps(item, indent=2))


def cmd_list_items(state_dir: str, phase: str, args: list[str]):
    """List all QR items with their status."""
    status_filter = None

    i = 0
    while i < len(args):
        if args[i] == "--status" and i + 1 < len(args):
            status_filter = args[i + 1].upper()
            i += 2
        else:
            i += 1

    qr_state = _load_qr_state_or_exit(state_dir, phase)

    for row in filtered_items_view(qr_state, status_filter):
        finding_str = f" | {row['finding']}" if row["finding"] else ""
        print(f"{row['id']}\t{row['status']}{finding_str}")


def cmd_summary(state_dir: str, phase: str, args: list[str]):
    """Print summary of QR state (counts by status)."""
    qr_state = _load_qr_state_or_exit(state_dir, phase)

    counts = status_counts(qr_state)
    total = sum(counts.values())
    print(f"Phase: {phase}")
    print(f"Total: {total}")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")


def cmd_assign_group(state_dir: str, phase: str, args: list[str]):
    """Assign QR item to a group.

    Usage: assign-group <item_id> --group-id <group_id>

    Atomic update with file locking. Group assignment is idempotent.
    Does not increment version (grouping is metadata, not verification).
    """
    if not args:
        error_exit("Usage: assign-group <item_id> --group-id <group_id>")

    item_id = args[0]
    group_id = None

    i = 1
    while i < len(args):
        if args[i] == "--group-id" and i + 1 < len(args):
            group_id = args[i + 1]
            i += 2
        else:
            i += 1

    if not group_id:
        error_exit("--group-id required")

    if not is_valid_group_id(group_id):
        error_exit(
            f"Invalid group_id '{group_id}'. Must be 'umbrella' or start with: parent-, component-, concern-, affinity-"
        )

    qr_path = get_qr_path(state_dir, phase)
    if not qr_path.exists():
        error_exit(f"QR state file not found: {qr_path}")

    with qr_write_lock(state_dir, phase):
        qr_state = load_qr_state_under_lock(qr_path)
        try:
            item = assign_group_in_state(qr_state, item_id, group_id)
        except ValueError as e:
            error_exit(str(e))
        save_qr_state_atomic(qr_path, qr_state)

    print_entity_result(
        EntityResult(id=item_id, version=item.get("version", 1), operation="updated")
    )


COMMANDS = {
    "update-item": cmd_update_item,
    "get-item": cmd_get_item,
    "list-items": cmd_list_items,
    "summary": cmd_summary,
    "assign-group": cmd_assign_group,
}


def cli(args: list[str] | None = None):
    """Main CLI entrypoint."""
    if args is None:
        args = sys.argv[1:]

    if not args:
        print(
            "Usage: uv run python -m skills.planner.cli.qr --state-dir <dir> --qr-phase <phase> <command> [args]"
        )
        print("")
        print("Global options:")
        print("  --state-dir <dir>   State directory (required)")
        print("  --qr-phase <phase>  QR phase name (required)")
        print("")
        print("Commands:")
        print("  update-item <id> --status <PASS|FAIL> [--finding <text>]")
        print("  get-item <id>")
        print("  list-items [--status <status>]")
        print("  summary")
        sys.exit(0)

    # Parse global options
    state_dir = None
    phase = None
    remaining_args = []

    i = 0
    while i < len(args):
        if args[i] == "--state-dir" and i + 1 < len(args):
            state_dir = args[i + 1]
            i += 2
        elif args[i] == "--qr-phase" and i + 1 < len(args):
            phase = args[i + 1]
            i += 2
        else:
            remaining_args.append(args[i])
            i += 1

    if not state_dir:
        error_exit("--state-dir required")
    if not phase:
        error_exit("--qr-phase required")

    if not remaining_args:
        error_exit("Command required")

    cmd = remaining_args[0]
    cmd_args = remaining_args[1:]

    # Handle batch command
    if cmd == "batch":
        methods = discover_methods(qr_commands)
        ctx = qr_commands.QRContext(state_dir=Path(state_dir), phase=phase)

        try:
            if cmd_args:
                requests = json.loads(cmd_args[0])
            else:
                requests = json.load(sys.stdin)

            if not isinstance(requests, list) or not all(isinstance(r, dict) for r in requests):
                error_exit("batch input must be a JSON array of {method, params} objects")

            results = batch_dispatch(methods, requests, ctx)
        except ValueError as e:
            error_exit(str(e))
        print(json.dumps(results, indent=2))
        return

    # Handle list-methods command
    if cmd == "list-methods":
        methods = discover_methods(qr_commands)
        print(json.dumps(list_methods(methods), indent=2))
        return

    if cmd not in COMMANDS:
        error_exit(f"Unknown command: {cmd}")

    try:
        COMMANDS[cmd](state_dir, phase, cmd_args)
    except (ValueError, OSError) as e:
        error_exit(str(e))


def main():
    cli()


if __name__ == "__main__":
    main()
