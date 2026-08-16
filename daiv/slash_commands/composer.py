from __future__ import annotations

from skills.models import GlobalSkill
from skills.services import list_builtins

from codebase.base import Scope
from slash_commands.registry import slash_command_registry


def composer_command_rows() -> list[dict[str, str]]:
    """Catalog for the chat composer's "/" autocomplete: the GLOBAL-scope slash commands
    followed by the global skills, custom shadowing built-ins of the same name.

    Skills are sourced as the skills dashboard sources them — ``list_builtins()`` plus the
    ``GlobalSkill`` rows — not from ``CUSTOM_SKILLS_PATH``, so a row whose disk tree went
    missing is listed here while ``/help``, which reads that tree, omits it. Per-repo
    skills live in the sandbox and cannot be listed at page render, as with ``/help``.
    """
    commands = sorted(
        (
            {"name": cls.command, "description": cls.description, "kind": "command"}
            for cls in slash_command_registry.get_commands(scope=Scope.GLOBAL)
        ),
        key=lambda row: row["name"],
    )

    skills = {entry["name"]: entry["description"] for entry in list_builtins()}
    skills.update(GlobalSkill.objects.values_list("name", "description"))

    return commands + [
        {"name": name, "description": description, "kind": "skill"} for name, description in sorted(skills.items())
    ]
