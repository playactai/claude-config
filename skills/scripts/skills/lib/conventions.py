"""Convention loading utilities.

Conventions are universal rules used by agents and skills. They live in
.claude/conventions/ (not skill-specific resources directories).

Available conventions:
- documentation.md: CLAUDE.md/README.md format specification
- structural.md: Code quality conventions (god object, testing, etc.)
- temporal.md: Comment hygiene (timeless present rule)
- severity.md: MUST/SHOULD/COULD severity definitions
- intent-markers.md: :PERF:/:UNSAFE: marker format
"""

from fnmatch import fnmatch
from functools import cache
from pathlib import Path

from skills.lib.io import read_text_or_exit

_registry_cache: dict | None = None


@cache
def get_convention(name: str) -> str:
    """Load convention from centralized store.

    Args:
        name: Convention filename (e.g., "temporal.md", "structural.md")

    Returns:
        Full content of the convention file

    Exits:
        With contextual error message if convention doesn't exist
    """
    # parents[4]: lib -> skills -> scripts -> skills -> .claude
    convention_path = Path(__file__).resolve().parents[4] / "conventions" / name
    return read_text_or_exit(convention_path, "loading convention")


def _parse_role_header(content: str) -> tuple[str, dict]:
    """Parse role header line (indent 0).

    Returns:
        (role_name, initial_role_dict)
    """
    role_name = content.split(":")[0].strip()
    return role_name, {}


def _parse_list_item(content: str) -> str:
    """Parse list item (starts with '-').

    Returns:
        Item value with quotes stripped
    """
    return content[1:].strip().strip("\"'")


def _parse_phase_item(content: str) -> tuple[str, list]:
    """Parse phase_specific phase header.

    Returns:
        (phase_name, empty_list)
    """
    phase_name = content.split(":")[0].strip()
    return phase_name, []


def _parse_indent_0_role(content: str, result: dict) -> tuple[str, None, None]:
    """Handle role header (indent 0).

    Returns:
        (current_role, current_key=None, current_phase=None)
    """
    role_name, role_dict = _parse_role_header(content)
    result[role_name] = role_dict
    return role_name, None, None


def _parse_indent_2_keys(content: str, current_role: str, result: dict) -> tuple[str, None]:
    """Handle second-level keys (indent 2).

    Returns:
        (current_key, current_phase=None)
    """
    key = content.split(":")[0].strip()
    if key not in ("receives", "phase_specific", "mode_specific", "rationale"):
        raise ValueError(
            f"Unrecognized REGISTRY.yaml key {key!r} under role {current_role!r}"
        )
    if key == "rationale":
        result[current_role][key] = content.split(":", 1)[1].strip().strip("\"'")
        return key, None

    # receives / phase_specific / mode_specific are block-style containers: their
    # items live on indented lines below. Any non-empty inline value (a flow-style
    # list "[a, b]" or map "{x: y}") cannot be represented by this subset parser
    # and would be silently dropped -- fail closed instead of losing a grant.
    inline = content.split(":", 1)[1].strip()
    if inline and inline not in ("[]", "{}"):
        raise ValueError(
            f"Flow-style value not supported for {key!r} under role {current_role!r}: "
            f"use block-style indented lines (got {inline!r})"
        )
    result[current_role][key] = [] if key == "receives" else {}

    return key, None


def _parse_indent_4_items(
    content: str, current_role: str, current_key: str, result: dict
) -> str | None:
    """Handle list items and phase/mode headers (indent 4).

    Returns:
        current_phase if a phase/mode header was parsed, None for consumed list items

    Raises:
        ValueError: If the line doesn't match any indent-4 pattern
    """
    if current_key == "receives" and content.startswith("-"):
        value = _parse_list_item(content)
        result[current_role][current_key].append(value)
        return None

    if current_key == "phase_specific" and ":" in content:
        phase_name, phase_list = _parse_phase_item(content)
        result[current_role][current_key][phase_name] = phase_list
        return phase_name

    if current_key == "mode_specific" and ":" in content:
        # mode_specific uses same structure as phase_specific
        mode_name, mode_list = _parse_phase_item(content)
        result[current_role][current_key][mode_name] = mode_list
        return mode_name

    raise ValueError(
        f"Unparseable REGISTRY.yaml line (indent 4): {content!r}"
    )


def _parse_indent_6_phase_items(
    content: str, current_role: str, current_key: str, current_phase: str | None, result: dict
) -> None:
    """Handle phase-specific and mode-specific list items (indent 6).

    Raises:
        ValueError: If the line doesn't match any indent-6 pattern
    """
    if (
        current_key in ("phase_specific", "mode_specific")
        and current_phase
        and content.startswith("-")
    ):
        value = _parse_list_item(content)
        result[current_role][current_key][current_phase].append(value)
    else:
        raise ValueError(
            f"Unparseable REGISTRY.yaml line (indent 6): {content!r}"
        )


def _validate_parsed_structure(result: dict) -> None:
    """Validate parsed registry structure.

    Args:
        result: Parsed registry dictionary

    Raises:
        ValueError: If structure is invalid
    """
    for role, config in result.items():
        # Each role must have receives or rationale
        if "receives" not in config and "rationale" not in config:
            raise ValueError(f"Role '{role}' missing 'receives' or 'rationale'")

        # phase_specific phases must have non-empty lists
        if "phase_specific" in config:
            for phase, items in config["phase_specific"].items():
                if not isinstance(items, list):
                    raise ValueError(f"Role '{role}' phase_specific.{phase} must be list")

        # mode_specific modes must have non-empty lists
        if "mode_specific" in config:
            for mode, items in config["mode_specific"].items():
                if not isinstance(items, list):
                    raise ValueError(f"Role '{role}' mode_specific.{mode} must be list")


def _parse_registry(text: str) -> dict:
    """Parse the role-convention registry.

    A purpose-built indentation parser for the small REGISTRY.yaml subset
    (roles -> receives / phase_specific / mode_specific / rationale). pyyaml is
    not a project dependency, so this is the single parser used both at runtime
    and by the CI drift-guard (validate_conventions.py) -- there is no second
    code path to drift against.
    """
    result: dict = {}
    current_role = None
    current_key = None
    current_phase = None

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped in ("---", "..."):
            continue

        indent = len(line) - len(line.lstrip())
        content = stripped

        if indent == 0 and ":" in content and not content.startswith("-"):
            current_role, current_key, current_phase = _parse_indent_0_role(content, result)
        elif indent == 2 and current_role and ":" in content:
            current_key, current_phase = _parse_indent_2_keys(content, current_role, result)
        elif indent == 4 and current_role and current_key:
            phase_update = _parse_indent_4_items(content, current_role, current_key, result)
            if phase_update is not None:
                current_phase = phase_update
        elif indent == 6 and current_role and current_key:
            _parse_indent_6_phase_items(content, current_role, current_key, current_phase, result)
        else:
            # No recognized indent/context matched. Raising (not skipping) keeps the
            # CI drift-guard fail-closed: a stray-indented or orphaned line that would
            # otherwise silently drop a role's convention grant is a hard error.
            raise ValueError(f"Unparseable REGISTRY.yaml line (indent {indent}): {content!r}")

    _validate_parsed_structure(result)
    return result


def get_registry() -> dict:
    """Load role-convention registry (cached)."""
    global _registry_cache
    if _registry_cache is None:
        registry_path = Path(__file__).resolve().parents[4] / "conventions" / "REGISTRY.yaml"
        _registry_cache = _parse_registry(registry_path.read_text(encoding="utf-8"))
    return _registry_cache


def get_conventions_for_role(
    role: str, phase: str | None = None, mode: str | None = None
) -> list[str]:
    """Return convention filenames for a role, optionally filtered by phase or mode."""
    registry = get_registry()
    role_config = registry.get(role, {})
    conventions = role_config.get("receives", [])

    if phase and "phase_specific" in role_config:
        phase_conventions = role_config["phase_specific"].get(phase, [])
        if phase_conventions:
            conventions = phase_conventions

    if mode and "mode_specific" in role_config:
        mode_conventions = role_config["mode_specific"].get(mode, [])
        if mode_conventions:
            conventions = mode_conventions

    return conventions


def validate_convention_access(role: str, convention: str) -> bool:
    """Check if role is allowed to access convention."""
    registry = get_registry()
    role_config = registry.get(role, {})
    receives = role_config.get("receives", [])

    # Check receives list (supports wildcards)
    return any(fnmatch(convention, pattern) for pattern in receives)
