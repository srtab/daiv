import re
import shlex
from dataclasses import dataclass


@dataclass
class SlashCommandCommand:
    """
    Structured result of a parsed bot command.
    """

    command: str
    args: list[str]
    raw: str


_COMMAND_RE_TEMPLATE = r"""
    (?<![\w@])           # negative look-behind: don't match e-mail addresses
    @(?P<bot>{bot})      # literal @bot-name mention
    \s+                  # at least one space or tab
    /                    # literal slash before command
    (?P<cmd>[^\n\r]+)    # capture the rest of the line (until newline)
"""


def _parse_command_match(text: str, *, pattern: str, flags: int) -> SlashCommandCommand | None:
    match = re.search(pattern, text, flags=flags)
    if not match:
        return None

    raw_line = match.group(0).strip()
    try:
        parts = shlex.split(match.group("cmd"))
    except ValueError:
        return None

    if not parts:
        return None

    command, *args = parts
    return SlashCommandCommand(command=command.lower(), args=args, raw=raw_line)


def parse_slash_command(note_body: str, bot_name: str) -> SlashCommandCommand | None:
    """
    Parse the first '@<bot_name> …' command in `note_body`.

    Args:
        note_body: The full text of a GitLab note / comment.
        bot_name: The bot mention to look for (case-insensitive).

    Returns:
        SlashCommandCommand if found, otherwise None.
    """
    pattern = _COMMAND_RE_TEMPLATE.format(bot=re.escape(bot_name))

    return _parse_command_match(note_body, pattern=pattern, flags=re.IGNORECASE | re.VERBOSE)


def parse_agent_slash_command(text: str, bot_name: str) -> SlashCommandCommand | None:
    """
    Parse slash commands for agent middleware.

    Supports both mention-based format (`@<bot_name> /command ...`) and bare slash commands (`/command ...`).

    Args:
        text: The message text to parse.
        bot_name: The bot mention to look for (case-insensitive).

    Returns:
        SlashCommandCommand if found, otherwise None.
    """
    # Try mention-based format first
    if result := parse_slash_command(text, bot_name):
        return result

    # Try bare slash command format
    # Look for lines starting with '/' (optionally preceded by whitespace)
    bare_pattern = r"^\s*/(?P<cmd>[^\n\r]+)"
    return _parse_command_match(text, pattern=bare_pattern, flags=re.MULTILINE)
