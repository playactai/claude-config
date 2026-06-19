"""Regression tests for the convention registry: role inference and the parser.

The convention-registry CI check infers a script's role from its path. These
tests pin that behavior, including the fix for role-named directories that
appear in the *checkout* path above the package root (which previously caused
false registry violations). They also pin the strict-parse behavior of
``_parse_registry`` (a stray-indented line is a hard error, not a silent skip).
"""

from pathlib import Path

import pytest

import validate_conventions as vc
from skills.lib import conventions

# Representative package root -- the skills_dir the CI passes in.
ROOT = Path("/repo/skills/scripts/skills")


@pytest.mark.parametrize(
    "relpath, expected",
    [
        ("planner/developer/exec_implement_execute.py", "developer"),
        ("planner/technical_writer/exec_docs_execute.py", "technical_writer"),
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
    script = root / "planner/technical_writer/exec_docs_execute.py"
    assert vc.infer_role_from_path(script, root) == "technical_writer"


def test_path_outside_package_root_is_unknown():
    """A script outside package_root is 'unknown' -- fail closed, never classified by ancestors."""
    script = Path("/elsewhere/developer/x.py")
    assert vc.infer_role_from_path(script, ROOT) == "unknown"


def test_parse_registry_raises_on_unrecognized_indent():
    """A stray/odd-indented line is a hard error, not a silent skip (D2)."""
    # 3-space indent under a role matches no 0/2/4/6 branch -> else -> raise.
    bad = "developer:\n   stray: value\n"
    with pytest.raises(ValueError, match="Unparseable REGISTRY"):
        conventions._parse_registry(bad)


def test_parse_registry_raises_on_malformed_indent4_receives():
    """Indent-4 line under receives missing '-' is a hard error, not silent skip."""
    bad = "developer:\n  receives:\n    temporal.md\n"
    with pytest.raises(ValueError, match="Unparseable REGISTRY"):
        conventions._parse_registry(bad)


def test_parse_registry_raises_on_malformed_indent4_phase_specific():
    """Indent-4 line under phase_specific missing ':' is a hard error."""
    bad = "developer:\n  receives: []\n  phase_specific:\n    plan_completeness\n"
    with pytest.raises(ValueError, match="Unparseable REGISTRY"):
        conventions._parse_registry(bad)


def test_parse_registry_raises_on_malformed_indent6():
    """Indent-6 line missing '-' under a phase is a hard error, not silent skip."""
    bad = (
        "quality_reviewer:\n"
        "  phase_specific:\n"
        "    plan_completeness:\n"
        "      structural.md\n"
    )
    with pytest.raises(ValueError, match="Unparseable REGISTRY"):
        conventions._parse_registry(bad)


def test_parse_registry_raises_on_indent6_under_receives():
    """Indent-6 list item under receives is a hard error, not silent drop."""
    bad = "developer:\n  receives:\n    - temporal.md\n      - documentation.md\n"
    with pytest.raises(ValueError, match="Unparseable REGISTRY"):
        conventions._parse_registry(bad)


def test_parse_registry_raises_on_inline_phase_specific_value():
    """Inline flow-style value under phase_specific is a hard error (was silently dropped)."""
    bad = "developer:\n  phase_specific:\n    plan_completeness: [structural.md]\n"
    with pytest.raises(ValueError, match="Flow-style value not supported"):
        conventions._parse_registry(bad)


def test_parse_registry_raises_on_inline_mode_specific_value():
    """Inline flow-style value under mode_specific is a hard error (was silently dropped)."""
    bad = "refactor:\n  mode_specific:\n    design: [temporal.md]\n"
    with pytest.raises(ValueError, match="Flow-style value not supported"):
        conventions._parse_registry(bad)


def test_committed_registry_parses_under_strict_parser():
    """The real REGISTRY.yaml stays parseable -- the strict parser isn't over-strict
    and no committed line is silently dropped."""
    conventions._registry_cache = None  # force a fresh parse of the on-disk file
    reg = conventions.get_registry()
    assert "developer" in reg
