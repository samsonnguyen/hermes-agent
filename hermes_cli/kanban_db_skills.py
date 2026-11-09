"""Task skill-list validation shared by Kanban task creation."""

from typing import Iterable, Optional


def normalize_task_skills(skills: Optional[Iterable[str]]) -> Optional[list[str]]:
    """Strip/dedupe skills and reject toolset names or comma-joined values."""
    if skills is None:
        return None
    from hermes_cli.kanban_db import KNOWN_TOOLSET_NAMES

    cleaned: list[str] = []
    seen: set[str] = set()
    toolset_typos: list[str] = []
    for skill in skills:
        if not skill:
            continue
        name = str(skill).strip()
        if not name:
            continue
        if "," in name:
            raise ValueError(
                f"skill name cannot contain comma: {name!r} "
                "(pass a list of separate names instead of a comma-joined string)"
            )
        if name.casefold() in KNOWN_TOOLSET_NAMES:
            toolset_typos.append(name)
        elif name not in seen:
            seen.add(name)
            cleaned.append(name)
    if toolset_typos:
        quoted = ", ".join(repr(name) for name in toolset_typos)
        noun = "is a toolset name" if len(toolset_typos) == 1 else "are toolset names"
        raise ValueError(
            f"{quoted} {noun}, not skill name(s). "
            "Put toolsets in the assignee profile's `toolsets:` config "
            "instead of per-task skills. Skills are named skill bundles "
            "(e.g. `blogwatcher`, `github-code-review`); toolsets are runtime "
            "capabilities (e.g. `web`, `browser`, `terminal`)."
        )
    return cleaned
