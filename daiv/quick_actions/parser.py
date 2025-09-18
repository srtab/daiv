import re
import shlex
from dataclasses import dataclass


@dataclass
class QuickActionCommand:
    """
    Structured result of a parsed bot command.
    """

    verb: str
    args: list[str]
    raw: str


_COMMAND_RE_TEMPLATE = r"""
    (?<![\w@])           # negative look-behind: don't match e-mail addresses
    @(?P<bot>{bot})      # literal @bot-name mention
    \s+                  # at least one space or tab
    (?P<cmd>[^\n\r]+)    # capture the rest of the line (until newline)
"""


def parse_quick_action(note_body: str, bot_name: str) -> QuickActionCommand | None:
    """
    Parse the first '@<bot_name> …' command in `note_body`.

    Args:
        note_body: The full text of a GitLab note / comment.
        bot_name: The bot mention to look for (case-insensitive).

    Returns:
        QuickActionCommand if found, otherwise None.
    """
    pattern = _COMMAND_RE_TEMPLATE.format(bot=re.escape(bot_name))

    match = re.search(pattern, note_body, flags=re.IGNORECASE | re.VERBOSE)
    if not match:
        return None

    raw_line = match.group(0).strip()
    try:
        parts = shlex.split(match.group("cmd"))
    except ValueError:
        return None

    if not parts:
        return None

    verb, *args = parts
    return QuickActionCommand(verb=verb.lower(), args=args, raw=raw_line)
