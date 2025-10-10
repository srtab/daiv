import re
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from core.constants import BOT_NAME

if TYPE_CHECKING:
    from codebase.base import Discussion, Note, User


def note_mentions_daiv(note_body: str, current_user: User) -> bool:
    """
    Check if the note body references the DAIV GitLab account.

    Returns True when the note contains:
    - Explicit user-mention (e.g. @daiv, @DAIV)
    - Bare textual reference to the bot name (e.g. DAIV please fix)

    Args:
        note_body: The note body text to check
        current_user: The current DAIV user

    Returns:
        bool: True if the note mentions DAIV, False otherwise
    """
    mention_pattern = rf"@{re.escape(current_user.username)}\b"
    return bool(re.search(mention_pattern, note_body, re.IGNORECASE))


def discussion_has_daiv_mentions(discussion: Discussion, current_user: User) -> bool:
    """
    Check if the discussion has any notes mentioning DAIV.

    Args:
        discussion: The discussion to check
        current_user: The current DAIV user

    Returns:
        bool: True if any note in the discussion mentions DAIV, False otherwise
    """
    return any(note_mentions_daiv(note.body, current_user) for note in discussion.notes)


def notes_to_messages(notes: list[Note], bot_user_id) -> list[AnyMessage]:
    """
    Convert a list of notes to a list of messages.

    Args:
        notes: List of notes
        bot_user_id: ID of the bot user

    Returns:
        List of messages
    """
    messages: list[AnyMessage] = []
    for note in notes:
        if note.author.id == bot_user_id:
            messages.append(AIMessage(id=note.id, content=note.body, name=BOT_NAME))
        else:
            messages.append(HumanMessage(id=note.id, content=note.body, name=note.author.username))
    return messages
