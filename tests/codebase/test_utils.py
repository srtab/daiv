from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage

from codebase.base import Note, NoteableType
from codebase.utils import notes_to_messages
from core.constants import BOT_NAME


class TestNotesToMessages:
    def test_notes_to_messages_with_bot_notes(self):
        bot_user_id = 1
        notes = [
            Note(
                author=Mock(id=1),
                body="Bot message 1",
                id=1,
                noteable_type=NoteableType.ISSUE,
                system=True,
                resolvable=True,
            ),
            Note(
                author=Mock(id=1),
                body="Bot message 2",
                id=2,
                noteable_type=NoteableType.ISSUE,
                system=True,
                resolvable=True,
            ),
        ]
        expected_messages = [
            AIMessage(content="Bot message 1", name=BOT_NAME),
            AIMessage(content="Bot message 2", name=BOT_NAME),
        ]
        result = notes_to_messages(notes, bot_user_id)
        assert result == expected_messages

    def test_notes_to_messages_with_human_notes(self):
        bot_user_id = 1
        notes = [
            Note(
                author=Mock(id=2, username="user1"),
                body="User message 1",
                id=1,
                noteable_type=NoteableType.ISSUE,
                system=False,
                resolvable=True,
            ),
            Note(
                author=Mock(id=3, username="user2"),
                body="User message 2",
                id=2,
                noteable_type=NoteableType.ISSUE,
                system=False,
                resolvable=True,
            ),
        ]
        expected_messages = [
            HumanMessage(content="User message 1", name="user1"),
            HumanMessage(content="User message 2", name="user2"),
        ]
        result = notes_to_messages(notes, bot_user_id)
        assert result == expected_messages

    def test_notes_to_messages_with_mixed_notes(self):
        bot_user_id = 1
        notes = [
            Note(
                author=Mock(id=1),
                body="Bot message",
                id=1,
                noteable_type=NoteableType.ISSUE,
                system=True,
                resolvable=True,
            ),
            Note(
                author=Mock(id=2, username="user1"),
                body="User message",
                id=2,
                noteable_type=NoteableType.ISSUE,
                system=False,
                resolvable=True,
            ),
        ]
        expected_messages = [
            AIMessage(content="Bot message", name=BOT_NAME),
            HumanMessage(content="User message", name="user1"),
        ]
        result = notes_to_messages(notes, bot_user_id)
        assert result == expected_messages
