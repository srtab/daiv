from __future__ import annotations

from django.utils.text import Truncator

from skills.services import list_global_skills

from codebase.base import Scope
from slash_commands.registry import slash_command_registry

# The menu clamps the subtitle to two lines; skill descriptions are written to trigger a
# model and run to GlobalSkill's 1024-char limit, so the untrimmed catalog ships kilobytes
# of never-painted text on every chat page render.
DESCRIPTION_CHARS = 240


def composer_command_rows() -> list[dict[str, str]]:
    """Catalog for the chat composer's "/" autocomplete: the GLOBAL-scope slash commands
    followed by the global skills.

    Per-repo skills live in the sandbox and cannot be listed at page render, as with ``/help``.
    """
    commands = sorted(
        (
            {"name": cls.command, "description": cls.description, "kind": "command"}
            for cls in slash_command_registry.get_commands(scope=Scope.GLOBAL)
        ),
        key=lambda row: row["name"],
    )

    return commands + [
        {"name": name, "description": Truncator(description).chars(DESCRIPTION_CHARS), "kind": "skill"}
        for name, description in sorted(list_global_skills().items())
    ]
