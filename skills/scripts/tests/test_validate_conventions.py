"""Regression tests for validate_conventions.infer_role_from_path.

The convention-registry CI check infers a script's role from its path. These
tests pin that behavior, including the fix for role-named directories that
appear in the *checkout* path above the package root (which previously caused
false registry violations).
"""

from pathlib import Path

import pytest

import validate_conventions as vc

# Representative package root -- the skills_dir the CI passes in.
ROOT = Path("/repo/skills/scripts/skills")


@pytest.mark.parametrize(
    "relpath, expected",
    [
        ("planner/developer/exec_implement_execute.py", "developer"),
        ("planner/technical_writer/exec_docs_qr_fix.py", "technical_writer"),
        ("planner/quality_reviewer/qr_decompose.py", "quality_reviewer"),
        ("planner/shared/qr/utils.py", "quality_reviewer"),
        ("refactor/refactor.py", "refactor"),
        ("planner/orchestrator/planner.py", "unknown"),
        ("lib/conventions.py", "unknown"),
    ],
)
def test_role_inferred_from_package_relative_dir(relpath, expected):
    """Role is taken from the package-relative role directory."""
    assert vc.infer_role_from_path(ROOT / relpath, ROOT) == expected


@pytest.mark.parametrize("ancestor", ["/tmp/developer", "/var/quality_reviewer", "/refactor/work"])
def test_role_named_ancestor_does_not_misclassify(ancestor):
    """A role-named directory ABOVE the package root must not win over the real role dir.

    Guards against a full-path scan: if inference scanned the whole absolute path,
    a role-named directory in the checkout path (e.g. /tmp/developer/...) would be
    returned instead of the script's real role. Scanning only the package-relative
    path prevents that.
    """
    root = Path(ancestor) / "co/skills/scripts/skills"
    script = root / "planner/technical_writer/exec_docs_qr_fix.py"
    assert vc.infer_role_from_path(script, root) == "technical_writer"


def test_path_outside_package_root_is_unknown():
    """A script outside package_root is 'unknown' -- fail closed, never classified by ancestors."""
    script = Path("/elsewhere/developer/x.py")
    assert vc.infer_role_from_path(script, ROOT) == "unknown"
