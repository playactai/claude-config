"""CI script: validates get_convention() calls match REGISTRY.yaml"""

import ast
import sys
from pathlib import Path

from skills.lib.conventions import get_registry, validate_convention_access


def extract_convention_calls(script_path: Path) -> tuple[list[tuple[str, int]], list[int]]:
    """Split get_convention() calls into statically-checkable vs. opaque.

    Returns (literal_calls, opaque_linenos):
      - literal_calls: (convention_name, line_number) for get_convention("literal"),
        which main() validates against the registry.
      - opaque_linenos: lines of get_convention(<non-string-literal>) calls. A
        variable/f-string argument cannot be resolved statically, so it would
        silently bypass the registry guard. We surface these as errors (fail loud)
        rather than skip them -- a real access could hide behind a variable.
    """
    tree = ast.parse(script_path.read_text())
    literal_calls = []
    opaque_linenos = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "get_convention"
        ):
            continue
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            literal_calls.append((first.value, node.lineno))
        else:
            opaque_linenos.append(node.lineno)
    return literal_calls, opaque_linenos


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
        calls, opaque_linenos = extract_convention_calls(script)
        role = infer_role_from_path(script, skills_dir)

        for convention, lineno in calls:
            if not validate_convention_access(role, convention):
                errors.append(
                    f"{script}:{lineno} - {role} accessing {convention} (not in registry)"
                )
        for lineno in opaque_linenos:
            errors.append(
                f"{script}:{lineno} - get_convention() with a non-literal argument cannot be "
                "statically validated; pass a string literal so the registry guard can check it"
            )

    if errors:
        print("Convention registry violations:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)

    print("Convention registry validation passed")


if __name__ == "__main__":
    main()
