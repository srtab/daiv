from codebase.clients.github.api.models import Issue


class TestGitHubIssueIsDaiv:
    """Test the is_daiv() method for GitHub Issue model."""

    def test_issue_is_daiv_with_daiv_label(self):
        """Test that is_daiv returns True when issue has 'daiv' label."""
        issue = Issue(
            id=1,
            number=1,
            title="Regular Issue",
            state="open",
            labels=[{"name": "bug"}, {"name": "daiv"}, {"name": "feature"}],
        )
        assert issue.is_daiv() is True

    def test_issue_is_daiv_with_daiv_auto_label(self):
        """Test that is_daiv returns True when issue has 'daiv-auto' label."""
        issue = Issue(
            id=1,
            number=1,
            title="Regular Issue",
            state="open",
            labels=[{"name": "bug"}, {"name": "daiv-auto"}, {"name": "feature"}],
        )
        assert issue.is_daiv() is True

    def test_issue_is_daiv_with_daiv_max_label(self):
        """Test that is_daiv returns True when issue has 'daiv-max' label."""
        issue = Issue(
            id=1,
            number=1,
            title="Regular Issue",
            state="open",
            labels=[{"name": "bug"}, {"name": "daiv-max"}, {"name": "feature"}],
        )
        assert issue.is_daiv() is True

    def test_issue_is_daiv_with_multiple_daiv_labels(self):
        """Test that is_daiv returns True when issue has multiple DAIV labels."""
        issue = Issue(
            id=1, number=1, title="Regular Issue", state="open", labels=[{"name": "daiv"}, {"name": "daiv-max"}]
        )
        assert issue.is_daiv() is True

    def test_issue_is_daiv_case_insensitive(self):
        """Test that is_daiv is case-insensitive for labels."""
        issue = Issue(
            id=1, number=1, title="Regular Issue", state="open", labels=[{"name": "DAIV"}, {"name": "feature"}]
        )
        assert issue.is_daiv() is True

    def test_issue_is_not_daiv_without_daiv_labels(self):
        """Test that is_daiv returns False when issue has no DAIV labels."""
        issue = Issue(
            id=1, number=1, title="Regular Issue", state="open", labels=[{"name": "bug"}, {"name": "feature"}]
        )
        assert issue.is_daiv() is False

    def test_issue_is_not_daiv_with_title_prefix(self):
        """Test that is_daiv returns False when issue title starts with 'daiv:' but has no label."""
        issue = Issue(
            id=1, number=1, title="daiv: Regular Issue", state="open", labels=[{"name": "bug"}, {"name": "feature"}]
        )
        assert issue.is_daiv() is False
