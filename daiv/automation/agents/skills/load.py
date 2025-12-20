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


def _parse_skill_metadata(
    skill_md_path: Path, *, cwd: Path | None = None, virtual_mode: bool = False
) -> SkillMetadata | None:
    """
    Parse YAML frontmatter from a SKILL.md file.

    Args:
        skill_md_path: Path to the SKILL.md file.
        cwd: Path to the current working directory. If None, the current working directory is not used.
        virtual_mode: Whether to use virtual mode. If True, the skill path is resolved to a virtual path.
            If False, the skill path is resolved to an absolute path.

    Returns:
        SkillMetadata with name, description, path, and scope, or None if parsing fails.
    """
    try:
        # Security: Check file size to prevent DoS attacks
        file_size = skill_md_path.stat().st_size
        if file_size > MAX_SKILL_FILE_SIZE:
            # Silently skip files that are too large
            return None

        content = skill_md_path.read_text(encoding="utf-8")

        # Match YAML frontmatter between --- delimiters
        frontmatter_pattern = r"^---\s*\n(.*?)\n---\s*\n"
        match = re.match(frontmatter_pattern, content, re.DOTALL)

        if not match:
            return None

        frontmatter = match.group(1)

        # Parse key-value pairs from YAML (simple parsing, no nested structures)
        metadata: dict[str, str] = {}
        for line in frontmatter.split("\n"):
            # Match "key: value" pattern
            kv_match = re.match(r"^(\w+):\s*(.+)$", line.strip())
            if kv_match:
                key, value = kv_match.groups()
                metadata[key] = value.strip()

        if "name" not in metadata or "description" not in metadata:
            return None

        if virtual_mode and cwd:
            skill_md_path = "/" + str(skill_md_path.relative_to(cwd))

        return SkillMetadata(
            name=metadata["name"], description=metadata["description"], path=skill_md_path, scope=metadata.get("scope")
        )

    except OSError, UnicodeDecodeError:
        return None


def list_skills(*, skills_dir: Path, cwd: Path | None = None, virtual_mode: bool = False) -> list[SkillMetadata]:
    """
    List all skills from a skills directory.

    Scans the skills directory for subdirectories containing SKILL.md files, parses YAML frontmatter, and returns
    skill metadata.

    Skills are organized as:
    {PROJECT_ROOT}/.daiv/skills/
    ├── skill-name/
    │   ├── SKILL.md        # Required: YAML frontmatter + instructions
    │   ├── checklist.md    # Optional: supporting documentation
    │   └── review.py       # Optional: helper Python script

    Args:
        skills_dir: Path to the skills directory.
        cwd: Path to the current working directory. If None, the current working directory is not used.
        virtual_mode: Whether to use virtual mode. If True, the skill path is resolved to a virtual path.
            If False, the skill path is resolved to an absolute path.
    Returns:
        List of skill metadata with name, description, path, and scope.
    """
    # Resolve base directory to canonical path for security checks
    try:
        resolved_base = skills_dir.resolve()
    except OSError, RuntimeError:
        return []

    skills: list[SkillMetadata] = []

    for skill_dir in skills_dir.iterdir():
        if not _is_safe_path(skill_dir, resolved_base):
            continue

        if not skill_dir.is_dir():
            continue

        skill_md_path = skill_dir / "SKILL.md"
        if not skill_md_path.exists():
            continue

        if not _is_safe_path(skill_md_path, resolved_base):
            continue

        if metadata := _parse_skill_metadata(skill_md_path, cwd=cwd, virtual_mode=virtual_mode):
            skills.append(metadata)

    return skills
