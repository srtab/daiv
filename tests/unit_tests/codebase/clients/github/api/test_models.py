from codebase.clients.github.api.models import Issue, Label


class TestGitHubIssueIsDaiv:
    """Test the is_daiv() method for GitHub Issue model."""

    def test_issue_is_daiv_with_daiv_label(self):
        """Test that is_daiv returns True when issue has 'daiv' label."""
        issue = Issue(
            id=1,
            number=1,
            title="Regular Issue",
            state="open",
            labels=[Label(id=1, name="bug"), Label(id=2, name="daiv"), Label(id=3, name="feature")],
        )
        assert issue.is_daiv() is True

    def test_issue_is_daiv_with_daiv_auto_label(self):
        """Test that is_daiv returns True when issue has 'daiv-auto' label."""
        issue = Issue(
            id=1,
            number=1,
            title="Regular Issue",
            state="open",
            labels=[Label(id=1, name="bug"), Label(id=2, name="daiv-auto"), Label(id=3, name="feature")],
        )
        assert issue.is_daiv() is True

    def test_issue_is_daiv_with_daiv_max_label(self):
        """Test that is_daiv returns True when issue has 'daiv-max' label."""
        issue = Issue(
            id=1,
            number=1,
            title="Regular Issue",
            state="open",
            labels=[Label(id=1, name="bug"), Label(id=2, name="daiv-max"), Label(id=3, name="feature")],
        )
        assert issue.is_daiv() is True

    def test_issue_is_daiv_with_multiple_daiv_labels(self):
        """Test that is_daiv returns True when issue has multiple DAIV labels."""
        issue = Issue(
            id=1,
            number=1,
            title="Regular Issue",
            state="open",
            labels=[Label(id=1, name="daiv"), Label(id=2, name="daiv-max")],
        )
        assert issue.is_daiv() is True

    def test_issue_is_daiv_case_insensitive(self):
        """Test that is_daiv is case-insensitive for labels."""
        issue = Issue(
            id=1,
            number=1,
            title="Regular Issue",
            state="open",
            labels=[Label(id=1, name="DAIV"), Label(id=2, name="feature")],
        )
        assert issue.is_daiv() is True

    def test_issue_is_not_daiv_without_daiv_labels(self):
        """Test that is_daiv returns False when issue has no DAIV labels."""
        issue = Issue(
            id=1,
            number=1,
            title="Regular Issue",
            state="open",
            labels=[Label(id=1, name="bug"), Label(id=2, name="feature")],
        )
        assert issue.is_daiv() is False

    def test_issue_is_not_daiv_with_title_prefix(self):
        """Test that is_daiv returns False when issue title starts with 'daiv:' but has no label."""
        issue = Issue(
            id=1,
            number=1,
            title="daiv: Regular Issue",
            state="open",
            labels=[Label(id=1, name="bug"), Label(id=2, name="feature")],
        )
        assert issue.is_daiv() is False
