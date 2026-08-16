from __future__ import annotations

from skills.models import GlobalSkill
from skills.services import list_builtins

from codebase.base import Scope
from slash_commands.registry import slash_command_registry


def composer_command_rows() -> list[dict[str, str]]:
    """Catalog for the chat composer's "/" autocomplete: the GLOBAL-scope slash commands
    followed by the global skills — the same set ``/help`` prints. Custom global skills
    shadow built-ins of the same name. Per-repo skills live in the sandbox and cannot be
    listed at page render, matching ``/help``.
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
