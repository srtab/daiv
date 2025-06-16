from langchain_core.messages import AIMessage, HumanMessage

from codebase.base import Discussion, Note, NoteableType, User
from codebase.utils import discussion_has_daiv_mentions, note_mentions_daiv, notes_to_messages
from core.constants import BOT_NAME


class TestNotesToMessages:
    def test_notes_to_messages_with_bot_notes(self):
        bot_user_id = 1
        notes = [
            Note(
                author=User(id=1, name="bot", username="bot"),
                body="Bot message 1",
                id=1,
                noteable_type=NoteableType.ISSUE,
                system=True,
                resolvable=True,
            ),
            Note(
                author=User(id=1, name="bot", username="bot"),
                body="Bot message 2",
                id=2,
                noteable_type=NoteableType.ISSUE,
                system=True,
                resolvable=True,
            ),
        ]
        expected_messages = [
            AIMessage(content="Bot message 1", name=BOT_NAME, id=1),
            AIMessage(content="Bot message 2", name=BOT_NAME, id=2),
        ]
        result = notes_to_messages(notes, bot_user_id)
        assert result == expected_messages

    def test_notes_to_messages_with_human_notes(self):
        bot_user_id = 1
        notes = [
            Note(
                author=User(id=2, username="user1", name="user1"),
                body="User message 1",
                id=1,
                noteable_type=NoteableType.ISSUE,
                system=False,
                resolvable=True,
            ),
            Note(
                author=User(id=3, username="user2", name="user1"),
                body="User message 2",
                id=2,
                noteable_type=NoteableType.ISSUE,
                system=False,
                resolvable=True,
            ),
        ]
        expected_messages = [
            HumanMessage(content="User message 1", name="user1", id=1),
            HumanMessage(content="User message 2", name="user2", id=2),
        ]
        result = notes_to_messages(notes, bot_user_id)
        assert result == expected_messages

    def test_notes_to_messages_with_mixed_notes(self):
        bot_user_id = 1
        notes = [
            Note(
                author=User(id=1, username="bot", name="bot"),
                body="Bot message",
                id=1,
                noteable_type=NoteableType.ISSUE,
                system=True,
                resolvable=True,
            ),
            Note(
                author=User(id=2, username="user1", name="user1"),
                body="User message",
                id=2,
                noteable_type=NoteableType.ISSUE,
                system=False,
                resolvable=True,
            ),
        ]
        expected_messages = [
            AIMessage(content="Bot message", name=BOT_NAME, id=1),
            HumanMessage(content="User message", name="user1", id=2),
        ]
        result = notes_to_messages(notes, bot_user_id)
        assert result == expected_messages


class TestNoteMentionsDaiv:
    def test_explicit_mention_lowercase(self):
        """Test that explicit @daiv mention is detected (case-insensitive)."""
        current_user = User(id=1, username="daiv", name="DAIV")
        note_body = "@daiv please review this code"
        assert note_mentions_daiv(note_body, current_user) is True

    def test_explicit_mention_uppercase(self):
        """Test that explicit @DAIV mention is detected (case-insensitive)."""
        current_user = User(id=1, username="daiv", name="DAIV")
        note_body = "@DAIV please review this code"
        assert note_mentions_daiv(note_body, current_user) is True

    def test_bare_text_mention_uppercase(self):
        """Test that bare DAIV text reference is ignored."""
        current_user = User(id=1, username="daiv", name="DAIV")
        note_body = "DAIV please fix this issue"
        assert note_mentions_daiv(note_body, current_user) is False

    def test_bare_text_mention_lowercase(self):
        """Test that bare daiv text reference is ignored (case-insensitive)."""
        current_user = User(id=1, username="daiv", name="DAIV")
        note_body = "daiv please fix this issue"
        assert note_mentions_daiv(note_body, current_user) is False

    def test_bare_text_mention_mixed_case(self):
        """Test that mixed case DAIV text reference is ignored."""
        current_user = User(id=1, username="daiv", name="DAIV")
        note_body = "Daiv please fix this issue"
        assert note_mentions_daiv(note_body, current_user) is False

    def test_no_mention(self):
        """Test that notes without DAIV mentions are not detected."""
        current_user = User(id=1, username="daiv", name="DAIV")
        note_body = "This looks good to me"
        assert note_mentions_daiv(note_body, current_user) is False

    def test_partial_word_not_detected(self):
        """Test that partial word matches are not detected."""
        current_user = User(id=1, username="daiv", name="DAIV")
        note_body = "This is a daivy comment"
        assert note_mentions_daiv(note_body, current_user) is False

    def test_mention_in_middle_of_sentence(self):
        """Test that mentions in the middle of sentences are detected."""
        current_user = User(id=1, username="daiv", name="DAIV")
        note_body = "I think @daiv should look at this"
        assert note_mentions_daiv(note_body, current_user) is True

    def test_different_username(self):
        """Test with different username."""
        current_user = User(id=1, username="bot", name="Bot")
        note_body = "@bot please review this code"
        assert note_mentions_daiv(note_body, current_user) is True


class TestDiscussionHasDAIVMentions:
    def test_discussion_with_daiv_mentions(self):
        """Test that discussions with DAIV mentions are detected."""
        current_user = User(id=1, username="daiv", name="DAIV")

        notes = [
            Note(
                id=1,
                author=User(id=2, username="reviewer", name="Reviewer"),
                body=f"This needs fixing @{current_user.username}",
                noteable_type=NoteableType.MERGE_REQUEST,
                system=False,
                resolvable=False,
            ),
            Note(
                id=2,
                author=current_user,
                body="I've updated the code",
                noteable_type=NoteableType.MERGE_REQUEST,
                system=False,
                resolvable=False,
            ),
        ]

        discussion = Discussion(id="discussion_1", notes=notes)
        assert discussion_has_daiv_mentions(discussion, current_user) is True

    def test_discussion_without_daiv_mentions(self):
        """Test that discussions without DAIV mentions are not detected."""
        current_user = User(id=1, username="daiv", name="DAIV")

        notes = [
            Note(
                id=1,
                author=User(id=2, username="reviewer", name="Reviewer"),
                body="This needs fixing",
                noteable_type=NoteableType.MERGE_REQUEST,
                system=False,
                resolvable=False,
            ),
            Note(
                id=2,
                author=User(id=3, username="other_reviewer", name="Other Reviewer"),
                body="I agree with the changes",
                noteable_type=NoteableType.MERGE_REQUEST,
                system=False,
                resolvable=False,
            ),
        ]

        discussion = Discussion(id="discussion_1", notes=notes)
        assert discussion_has_daiv_mentions(discussion, current_user) is False

    def test_empty_discussion(self):
        """Test that empty discussions return False."""
        current_user = User(id=1, username="daiv", name="DAIV")
        discussion = Discussion(id="discussion_1", notes=[])
        assert discussion_has_daiv_mentions(discussion, current_user) is False

    def test_discussion_with_daiv_mentions_on_other_notes(self):
        """Test that discussions with DAIV mentions on other notes are detected."""
        current_user = User(id=1, username="daiv", name="DAIV")

        notes = [
            Note(
                id=1,
                author=User(id=2, username="reviewer", name="Reviewer"),
                body="This needs fixing",
                noteable_type=NoteableType.MERGE_REQUEST,
                system=False,
                resolvable=False,
            ),
            Note(
                id=2,
                author=User(id=3, username="other_reviewer", name="Other Reviewer"),
                body="I agree with the changes",
                noteable_type=NoteableType.MERGE_REQUEST,
                system=False,
                resolvable=False,
            ),
            Note(
                id=3,
                author=User(id=2, username="reviewer", name="Reviewer"),
                body=f"This needs fixing @{current_user.username}",
                noteable_type=NoteableType.MERGE_REQUEST,
                system=False,
                resolvable=False,
            ),
        ]

        discussion = Discussion(id="discussion_1", notes=notes)
        assert discussion_has_daiv_mentions(discussion, current_user) is True

    def test_discussion_with_daiv_mentions_multilines(self):
        """Test that discussions with DAIV mentions on multiple lines are detected."""
        current_user = User(id=1, username="daiv", name="DAIV")

        notes = [
            Note(
                id=1,
                author=User(id=2, username="reviewer", name="Reviewer"),
                body=f"This needs fixing\n\nThis needs fixing @{current_user.username}\n\nThis needs fixing",
                noteable_type=NoteableType.MERGE_REQUEST,
                system=False,
                resolvable=False,
            )
        ]

        discussion = Discussion(id="discussion_1", notes=notes)
        assert discussion_has_daiv_mentions(discussion, current_user) is True
