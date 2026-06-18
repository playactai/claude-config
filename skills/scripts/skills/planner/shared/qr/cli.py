"""CLI utilities for QR workflows.

Moved from lib/workflow/cli.py to planner/shared/qr/cli.py.

Note: --qr-iteration and --qr-fail removed. Iteration is stored in
qr-{phase}.json; fix mode detected by file state inspection. The verify runner
declares its own --qr-item (an append list); the orchestrators only need
--qr-status.
"""

import argparse


def add_qr_args(parser: argparse.ArgumentParser) -> None:
    """Add standard QR verification arguments to argument parser.

    Used by the orchestrator scripts (planner.py, executor.py) for the
    --qr-status gate flag.
    """
    parser.add_argument(
        "--qr-status", type=str, choices=["pass", "fail"], help="QR result for gate steps"
    )
