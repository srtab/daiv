from codebase.base import Discussion, Note, NoteableType, User
from codebase.utils import discussion_has_daiv_notes, note_mentions_daiv


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
        """Test that bare DAIV text reference is detected."""
        current_user = User(id=1, username="daiv", name="DAIV")
        note_body = "DAIV please fix this issue"
        assert note_mentions_daiv(note_body, current_user) is True

    def test_bare_text_mention_lowercase(self):
        """Test that bare daiv text reference is detected (case-insensitive)."""
        current_user = User(id=1, username="daiv", name="DAIV")
        note_body = "daiv please fix this issue"
        assert note_mentions_daiv(note_body, current_user) is True

    def test_bare_text_mention_mixed_case(self):
        """Test that mixed case DAIV text reference is detected."""
        current_user = User(id=1, username="daiv", name="DAIV")
        note_body = "Daiv please fix this issue"
        assert note_mentions_daiv(note_body, current_user) is True

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


class TestDiscussionHasDaivNotes:
    def test_discussion_with_daiv_notes(self):
        """Test that discussions with DAIV-authored notes are detected."""
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
                author=current_user,
                body="I've updated the code",
                noteable_type=NoteableType.MERGE_REQUEST,
                system=False,
                resolvable=False,
            ),
        ]

        discussion = Discussion(id="discussion_1", notes=notes)
        assert discussion_has_daiv_notes(discussion, current_user) is True

    def test_discussion_without_daiv_notes(self):
        """Test that discussions without DAIV-authored notes are not detected."""
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
        assert discussion_has_daiv_notes(discussion, current_user) is False

    def test_empty_discussion(self):
        """Test that empty discussions return False."""
        current_user = User(id=1, username="daiv", name="DAIV")
        discussion = Discussion(id="discussion_1", notes=[])
        assert discussion_has_daiv_notes(discussion, current_user) is False

    def test_discussion_only_daiv_notes(self):
        """Test that discussions with only DAIV notes are detected."""
        current_user = User(id=1, username="daiv", name="DAIV")

        notes = [
            Note(
                id=1,
                author=current_user,
                body="Initial comment",
                noteable_type=NoteableType.MERGE_REQUEST,
                system=False,
                resolvable=False,
            ),
            Note(
                id=2,
                author=current_user,
                body="Follow-up comment",
                noteable_type=NoteableType.MERGE_REQUEST,
                system=False,
                resolvable=False,
            ),
        ]

        discussion = Discussion(id="discussion_1", notes=notes)
        assert discussion_has_daiv_notes(discussion, current_user) is True
