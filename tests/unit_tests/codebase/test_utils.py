import logging

import pytest

from codebase.base import User
from codebase.utils import (
    diff_line_stats,
    is_git_auth_error_text,
    is_git_ref_not_found_text,
    note_mentions_daiv,
    redact_diff_content,
)


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


class TestRedactDiffContent:
    def test_redacts_omitted_file_content(self):
        diff = (
            "diff --git a/secrets.lock b/secrets.lock\n"
            "index 1111111..2222222 100644\n"
            "--- a/secrets.lock\n"
            "+++ b/secrets.lock\n"
            "@@ -1 +1 @@\n"
            "-old secret\n"
            "+new secret\n"
        )
        redacted = redact_diff_content(diff, ("*.lock",))
        assert "old secret" not in redacted
        assert "new secret" not in redacted
        assert "intentionally excluded" in redacted

    def test_keeps_non_omitted_file_content(self):
        diff = (
            "diff --git a/app.py b/app.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-x = 1\n"
            "+x = 2\n"
        )
        redacted = redact_diff_content(diff, ("*.lock",))
        assert "x = 2" in redacted

    def test_redacts_omitted_file_alongside_hunkless_empty_file_section(self):
        """A hunkless empty-file-add section (``diff --git`` + ``new file mode`` + ``index``, no ``@@``)
        co-present with an omitted file must not derail redaction — this is the security property the
        ``_append_untracked`` canonical-diff fix guarantees. Before that fix the empty-file section was
        followed by an injected blank line, which made ``unidiff`` abort the whole parse; redaction was
        then skipped and the ``*.lock`` content leaked unredacted to the diff-to-metadata model.
        """
        diff = (
            # hunkless empty-file addition, butted directly against the next section (canonical output)
            "diff --git a/__init__.py b/__init__.py\n"
            "new file mode 100644\n"
            "index 0000000..e69de29\n"
            "diff --git a/secrets.lock b/secrets.lock\n"
            "index 1111111..2222222 100644\n"
            "--- a/secrets.lock\n"
            "+++ b/secrets.lock\n"
            "@@ -1 +1 @@\n"
            "-old secret\n"
            "+new secret\n"
        )
        redacted = redact_diff_content(diff, ("*.lock",))
        assert "old secret" not in redacted
        assert "new secret" not in redacted
        assert "intentionally excluded" in redacted
        assert "__init__.py" in redacted  # the empty-file section survives untouched

    def test_tolerates_truncated_diff(self, caplog):
        """A diff cut mid-hunk (truncated upstream, then folded onto a later file section) must not raise.

        ``redact_diff_content`` feeds the non-critical diff-to-metadata step; a parse failure
        there must never abort ``GitMiddleware.aafter_agent`` and discard the agent's committed
        work. It degrades to best-effort (returns the diff text) instead — and logs a warning,
        which is the only signal that redaction was bypassed.
        """
        # views.py's hunk header declares 60 lines, but the body was truncated upstream after a
        # few; the next (untracked) django.po section is then folded on by ``_append_untracked``.
        truncated = (
            "diff --git a/daiv/activity/views.py b/daiv/activity/views.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/daiv/activity/views.py\n"
            "+++ b/daiv/activity/views.py\n"
            "@@ -1,60 +1,60 @@\n"
            " ctx 0\n"
            " ctx 1\n"
            " ctx 2\n"
            "\n"
            "diff --git a/daiv/activity/locale/pt/LC_MESSAGES/django.po"
            " b/daiv/activity/locale/pt/LC_MESSAGES/django.po\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/daiv/activity/locale/pt/LC_MESSAGES/django.po\n"
            "@@ -0,0 +1,1 @@\n"
            '+msgid "x"\n'
        )
        with caplog.at_level(logging.WARNING, logger="daiv.codebase"):
            result = redact_diff_content(truncated, ("*.lock",))
        assert isinstance(result, str)
        assert "django.po" in result
        # The degrade must be loud: a dropped/downgraded warning would let unredacted diffs pass silently.
        assert any(r.levelno == logging.WARNING for r in caplog.records)


class TestDiffLineStats:
    """The arithmetic behind the composer's ``+x −y`` pill."""

    def test_counts_content_lines_and_files(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,2 +1,3 @@\n"
            " context\n"
            "+added one\n"
            "+added two\n"
            "-removed one\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -0,0 +1 @@\n"
            "+added three\n"
        )
        stats = diff_line_stats(diff)
        assert (stats.lines_added, stats.lines_removed, stats.files_changed) == (3, 1, 2)

    def test_file_headers_are_not_content(self):
        """``+++``/``---`` open every file section; counting them would inflate every diff
        by two lines per file."""
        stats = diff_line_stats("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n")
        assert (stats.lines_added, stats.lines_removed) == (0, 0)

    def test_empty_diff_is_all_zeros(self):
        stats = diff_line_stats("")
        assert (stats.lines_added, stats.lines_removed, stats.files_changed) == (0, 0, 0)

    def test_content_lines_that_look_like_headers_still_count(self):
        """``+++i;`` and ``-- comment`` are real content. Only a line's position — inside a
        hunk or not — separates content from the ``+++``/``---`` file headers."""
        diff = "diff --git a/a.c b/a.c\n--- a/a.c\n+++ b/a.c\n@@ -1,2 +1,2 @@\n+++i;\n--- audit trail\n"
        stats = diff_line_stats(diff)
        assert (stats.lines_added, stats.lines_removed) == (1, 1)


class IsGitAuthErrorTextTest:
    """Direct contract test for the shared marker set used by both the clone-retry self-heal
    (codebase.clients.gitlab) and the push-failure classifier (automation.agent.workspace.git_manager).
    A marker dropped here silently widens or narrows both behaviours, so pin them explicitly."""

    @pytest.mark.parametrize(
        "text",
        [
            "fatal: Authentication failed for 'https://gitlab.example/acme/repo.git'",
            "remote: HTTP Basic: Access denied",  # subsumed by "access denied"
            "The requested URL returned error: 403",
            "fatal: could not read Username for 'https://...': terminal prompts disabled",
            "remote: Permission denied",
            "remote: You are not authorized to push to this repository",
            "AUTHENTICATION FAILED",  # case-insensitive
        ],
    )
    def test_auth_markers_return_true(self, text):
        assert is_git_auth_error_text(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "fatal: unable to access 'https://...': Could not resolve host",
            "fatal: couldn't find remote ref refs/heads/missing",
            "",
            "Everything up-to-date",
        ],
    )
    def test_non_auth_text_returns_false(self, text):
        assert is_git_auth_error_text(text) is False


@pytest.mark.parametrize(
    "text,expected",
    [
        ("fatal: Remote branch chore/x not found in upstream origin", True),
        ("Remote branch feature-1 not found in upstream origin\n", True),
        ("fatal: could not read Username for 'https://...'", False),  # auth, not a missing ref
        ("fatal: unable to access '...': Could not resolve host", False),  # network
        ("", False),
    ],
)
def test_is_git_ref_not_found_text(text, expected):
    assert is_git_ref_not_found_text(text) is expected
