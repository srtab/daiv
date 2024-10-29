from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from codebase.base import Note
from core.constants import BOT_NAME


def notes_to_messages(notes: list[Note], bot_user_id) -> list[AnyMessage]:
    """
    Convert a list of notes to a list of messages.
    """
    messages: list[AnyMessage] = []
    for note in notes:
        if note.author.id == bot_user_id:
            messages.append(AIMessage(id=note.id, content=note.body, name=BOT_NAME))
        else:
            messages.append(HumanMessage(id=note.id, content=note.body, name=note.author.username))
    return messages
