"""CI script: validates get_convention() calls match REGISTRY.yaml"""

import ast
import sys
from pathlib import Path

from skills.lib.conventions import get_registry, validate_convention_access


def extract_convention_calls(script_path: Path) -> list[tuple[str, int]]:
    """Extract (convention_name, line_number) from get_convention() calls."""
    tree = ast.parse(script_path.read_text())
    calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "get_convention"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            calls.append((node.args[0].value, node.lineno))
    return calls


# Role directory name -> registry role. Full directory names plus the `qr` alias
# for the shared QR helpers under planner/shared/qr/. REGISTRY.yaml is the
# authority for which role names exist; main() guards this map against it.
ROLE_BY_DIR = {
    "quality_reviewer": "quality_reviewer",
    "qr": "quality_reviewer",
    "developer": "developer",
    "technical_writer": "technical_writer",
    "refactor": "refactor",
}


def infer_role_from_path(script_path: Path, package_root: Path) -> str:
    """Infer AgentRole from a script's location within the package.

    Match against the role directory name (see ROLE_BY_DIR). Only the
    package-relative path is scanned, so a role-named directory in the *checkout*
    path above package_root (e.g. /tmp/developer/...) cannot misclassify a script
    whose real role directory lives inside the package. A script outside
    package_root is "unknown" -- fail closed, never trust an ancestor directory.
    """
    try:
        parts = script_path.relative_to(package_root).parts
    except ValueError:
        return "unknown"
    for part in parts:
        if part in ROLE_BY_DIR:
            return ROLE_BY_DIR[part]
    return "unknown"


def main():
    registry = get_registry()
    stray_roles = set(ROLE_BY_DIR.values()) - set(registry)
    if stray_roles:
        print(f"ROLE_BY_DIR maps to roles absent from REGISTRY.yaml: {sorted(stray_roles)}")
        sys.exit(1)

    skills_dir = Path(__file__).parent / "skills"
    errors = []

    for script in skills_dir.rglob("*.py"):
        calls = extract_convention_calls(script)
        role = infer_role_from_path(script, skills_dir)

        for convention, lineno in calls:
            if not validate_convention_access(role, convention):
                errors.append(
                    f"{script}:{lineno} - {role} accessing {convention} (not in registry)"
                )

    if errors:
        print("Convention registry violations:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)

    print("Convention registry validation passed")


if __name__ == "__main__":
    main()
