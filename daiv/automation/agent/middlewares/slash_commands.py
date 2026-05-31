from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from deepagents.middleware.skills import SkillMetadata, _parse_skill_metadata

from automation.agent.conf import settings as agent_settings
from automation.agent.constants import BUILTIN_SKILLS_PATH

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("daiv.tools")


def _load_global_skill_metadata() -> list[SkillMetadata]:
    """Read builtin + custom *global* skill metadata from disk (name/description).

    Used by :class:`SlashCommandMiddleware` for ``/help``, which runs before the sandbox
    session exists — so it cannot read per-repo skills (those live in the sandbox). The
    agent's ``<available_skills>`` system prompt still enumerates everything once the loop
    runs; this is only the ``/help`` listing. Custom skills override builtins of the same
    name (later source wins).
    """
    skills: dict[str, SkillMetadata] = {}
    roots: list[Path] = [BUILTIN_SKILLS_PATH]
    custom = agent_settings.CUSTOM_SKILLS_PATH
    if custom is not None and custom.is_dir():
        roots.append(custom)

    for root in roots:
        try:
            children = sorted(root.iterdir())
        except OSError:
            logger.warning("Could not read global skills root '%s' for /help", root, exc_info=True)
            continue
        for skill_dir in children:
            if not skill_dir.is_dir() or skill_dir.name.startswith(".") or skill_dir.name == "__pycache__":
                continue
            skill_md = skill_dir / "SKILL.md"
            try:
                content = skill_md.read_text(encoding="utf-8")
            except OSError:
                continue
            meta = _parse_skill_metadata(content, str(skill_md), skill_dir.name)
            if meta is not None:
                skills[meta["name"]] = meta
    return list(skills.values())
