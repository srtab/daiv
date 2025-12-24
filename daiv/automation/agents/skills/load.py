"""
Skill loader for parsing and loading agent skills from SKILL.md files.

This module implements Anthropic's agent skills pattern with YAML frontmatter parsing.
Each skill is a directory containing a SKILL.md file with:
- YAML frontmatter (name, description required)
- Markdown instructions for the agent
- Optional supporting files (scripts, configs, etc.)

Example SKILL.md structure:
```markdown
---
name: web-research
description: Structured approach to conducting thorough web research
---

# Web Research Skill

## When to Use
- User asks you to research a topic
...
```
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from daiv.settings.components import PROJECT_DIR

if TYPE_CHECKING:
    from pathlib import Path

    from deepagents.backends.protocol import BACKEND_TYPES


MAX_SKILL_FILE_SIZE = 1 * 1024 * 1024
BUILTIN_SKILLS_DIR = PROJECT_DIR / "automation" / "agents" / "skills" / "builtin"


@dataclass(frozen=True)
class SkillMetadata:
    """Metadata for a skill."""

    name: str
    """Name of the skill."""

    description: str
    """Description of what the skill does."""

    path: str
    """Path to the SKILL.md file as a string."""

    scope: Literal["issue", "merge_request"] | None = None
    """Scope of the skill. If None, the skill is applicable to both issues and merge requests."""


def _is_safe_path(path: Path, base_dir: Path) -> bool:
    """
    Check if a path is safely contained within base_dir.

    This prevents directory traversal attacks via symlinks or path manipulation.
    The function resolves both paths to their canonical form (following symlinks) and verifies that the target path
    is within the base directory.

    Args:
        path: The path to validate
        base_dir: The base directory that should contain the path

    Returns:
        True if the path is safely within base_dir, False otherwise

    Example:
        >>> base = Path("/home/user/daiv/skills")
        >>> safe = Path("/home/user/daiv/skills/web-research/SKILL.md")
        >>> unsafe = Path("/home/user/daiv/skills/../../.ssh/id_rsa")
        >>> _is_safe_path(safe, base)
        True
        >>> _is_safe_path(unsafe, base)
        False
    """
    try:
        # Resolve both paths to their canonical form (follows symlinks)
        resolved_path = path.resolve()
        resolved_base = base_dir.resolve()

        # Check if the resolved path is within the base directory
        # This catches symlinks that point outside the base directory
        resolved_path.relative_to(resolved_base)
        return True
    except ValueError:
        # Path is not relative to base_dir (outside the directory)
        return False
    except OSError, RuntimeError:
        # Error resolving paths (e.g., circular symlinks, too many levels)
        return False


def _parse_skill_metadata(skill_md_path: str, *, backend: BACKEND_TYPES) -> SkillMetadata | None:
    """
    Parse YAML frontmatter from a SKILL.md file.

    Args:
        skill_md_path: Path to the SKILL.md file.
        backend: The backend to use for reading the skills.

    Returns:
        SkillMetadata with name, description, path, and scope, or None if parsing fails.
    """
    try:
        content = backend.read(skill_md_path)

        # Match YAML frontmatter between --- delimiters
        # Pattern handles optional line numbers at the start (e.g., "1 ---", "  2 name: value")
        frontmatter_pattern = r"^(?:\s*\d+\s+)?---\s*\n(.*?)\n(?:\s*\d+\s+)?---\s*\n"
        match = re.match(frontmatter_pattern, content, re.DOTALL)

        if not match:
            return None

        frontmatter = match.group(1)

        # Parse key-value pairs from YAML (simple parsing, no nested structures)
        metadata: dict[str, str] = {}
        for line in frontmatter.split("\n"):
            # Strip line numbers at the start (e.g., "2 name: value" -> "name: value")
            line_stripped = re.sub(r"^\s*\d+\s+", "", line.strip())
            # Match "key: value" pattern
            kv_match = re.match(r"^(\w+):\s*(.+)$", line_stripped)
            if kv_match:
                key, value = kv_match.groups()
                metadata[key] = value.strip()

        if "name" not in metadata or "description" not in metadata:
            return None

        return SkillMetadata(
            name=metadata["name"], description=metadata["description"], path=skill_md_path, scope=metadata.get("scope")
        )

    except OSError, UnicodeDecodeError:
        return None


def list_skills(*, project_skills_dir: str, builtin_skills_dir: str, backend: BACKEND_TYPES) -> list[SkillMetadata]:
    """
    List all skills from a skills directory.

    Scans the skills directory for subdirectories containing SKILL.md files, parses YAML frontmatter, and returns
    skill metadata.

    Skills are organized as:
    - Builtin skills (outside of repository): /skills/
    - Project skills (in repository): .daiv/skills/
    ├── skill-name/
    │   ├── SKILL.md        # Required: YAML frontmatter + instructions
    │   ├── checklist.md    # Optional: supporting documentation
    │   └── review.py       # Optional: helper Python script

    Args:
        project_skills_dir: Path to the project skills directory.
        builtin_skills_dir: Path to the builtin skills directory.
        backend: The backend to use for reading the skills.
    Returns:
        List of skill metadata with name, description, path, and scope.
    """
    skills: dict[str, SkillMetadata] = {}

    for base_skills_dir in [project_skills_dir, builtin_skills_dir]:
        for skill_dir in backend.ls_info(base_skills_dir):
            if not skill_dir["is_dir"]:
                continue

            for file in backend.ls_info(skill_dir["path"]):
                if (
                    not file["is_dir"]
                    and file["path"].endswith("/SKILL.md")
                    and file["size"] < MAX_SKILL_FILE_SIZE
                    and (metadata := _parse_skill_metadata(file["path"], backend=backend))
                    and metadata.name not in skills
                ):
                    skills[metadata.name] = metadata
    return list(skills.values())
