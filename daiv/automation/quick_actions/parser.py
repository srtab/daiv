import re
from typing import TypedDict


class QuickActionCommand(TypedDict):
    """Type definition for a parsed quick action command."""
    identifier: str
    params: str | None


def parse_quick_actions(note_body: str) -> list[QuickActionCommand]:
    """
    Parse quick actions from a note body using GitLab-style slash commands.

    Supports commands like:
    - /hello
    - /assign @user
    - /label bug

    Args:
        note_body: The note body text to parse.

    Returns:
        List of dictionaries with 'identifier' and 'params' keys.
    """
    if not note_body:
        return []

    # Remove code blocks (between triple backticks) to avoid parsing commands inside code
    code_block_pattern = r'```[\s\S]*?```'
    note_without_code_blocks = re.sub(code_block_pattern, '', note_body, flags=re.MULTILINE)

    # Pattern to match slash commands at the beginning of lines or after whitespace
    # Captures the command identifier and optional parameters
    command_pattern = r'(?:^|\s)/(\w+)(?:\s+(.+?))?(?=\s*$|\s*/\w+)'

    commands = []
    for match in re.finditer(command_pattern, note_without_code_blocks, re.MULTILINE):
        identifier = match.group(1)
        params = match.group(2).strip() if match.group(2) else None

        commands.append(QuickActionCommand(identifier=identifier, params=params))

    return commands