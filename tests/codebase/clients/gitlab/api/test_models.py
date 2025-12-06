from codebase.clients.gitlab.api.models import Issue, Label, MergeRequest


class TestGitLabIssueIsDaiv:
    """Test the is_daiv() method for GitLab Issue model."""

    def test_issue_is_daiv_with_daiv_label(self):
        """Test that is_daiv returns True when issue has 'daiv' label."""
        issue = Issue(
            id=1,
            iid=1,
            title="Regular Issue",
            description="Test Issue",
            labels=[Label(title="bug"), Label(title="daiv"), Label(title="feature")],
            state="open",
            type="Issue",
            assignee_id=1,
        )
        assert issue.is_daiv() is True

    def test_issue_is_daiv_with_daiv_auto_label(self):
        """Test that is_daiv returns True when issue has 'daiv-auto' label."""
        issue = Issue(
            id=1,
            iid=1,
            title="Regular Issue",
            description="Test Issue",
            labels=[Label(title="bug"), Label(title="daiv-auto"), Label(title="feature")],
            state="open",
            type="Issue",
            assignee_id=1,
        )
        assert issue.is_daiv() is True

    def test_issue_is_daiv_with_daiv_max_label(self):
        """Test that is_daiv returns True when issue has 'daiv-max' label."""
        issue = Issue(
            id=1,
            iid=1,
            title="Regular Issue",
            description="Test Issue",
            labels=[Label(title="bug"), Label(title="daiv-max"), Label(title="feature")],
            state="open",
            type="Issue",
            assignee_id=1,
        )
        assert issue.is_daiv() is True

    def test_issue_is_daiv_with_multiple_daiv_labels(self):
        """Test that is_daiv returns True when issue has multiple DAIV labels."""
        issue = Issue(
            id=1,
            iid=1,
            title="Regular Issue",
            description="Test Issue",
            labels=[Label(title="daiv-auto"), Label(title="daiv-max")],
            state="open",
            type="Issue",
            assignee_id=1,
        )
        assert issue.is_daiv() is True

    def test_issue_is_daiv_case_insensitive(self):
        """Test that is_daiv is case-insensitive for labels."""
        issue = Issue(
            id=1,
            iid=1,
            title="Regular Issue",
            description="Test Issue",
            labels=[Label(title="DAIV-AUTO"), Label(title="feature")],
            state="open",
            type="Issue",
            assignee_id=1,
        )
        assert issue.is_daiv() is True

    def test_issue_is_not_daiv_without_daiv_labels(self):
        """Test that is_daiv returns False when issue has no DAIV labels."""
        issue = Issue(
            id=1,
            iid=1,
            title="Regular Issue",
            description="Test Issue",
            labels=[Label(title="bug"), Label(title="feature")],
            state="open",
            type="Issue",
            assignee_id=1,
        )
        assert issue.is_daiv() is False

    def test_issue_is_not_daiv_with_title_prefix(self):
        """Test that is_daiv returns False when issue title starts with 'daiv:' but has no label."""
        issue = Issue(
            id=1,
            iid=1,
            title="daiv: Regular Issue",
            description="Test Issue",
            labels=[Label(title="bug"), Label(title="feature")],
            state="open",
            type="Issue",
            assignee_id=1,
        )
        assert issue.is_daiv() is False


class TestGitLabMergeRequestIsDaiv:
    """Test the is_daiv() method for GitLab MergeRequest model."""

    def test_mr_is_daiv_with_daiv_label(self):
        """Test that is_daiv returns True when MR has 'daiv' label."""
        mr = MergeRequest(
            id=1,
            iid=1,
            title="Regular MR",
            labels=[Label(title="bug"), Label(title="daiv"), Label(title="feature")],
            state="open",
            source_branch="main",
            target_branch="feature",
        )
        assert mr.is_daiv() is True

    def test_mr_is_daiv_with_daiv_auto_label(self):
        """Test that is_daiv returns True when MR has 'daiv-auto' label."""
        mr = MergeRequest(
            id=1,
            iid=1,
            title="Regular MR",
            labels=[Label(title="daiv-auto")],
            state="open",
            source_branch="main",
            target_branch="feature",
        )
        assert mr.is_daiv() is True

    def test_mr_is_daiv_with_daiv_max_label(self):
        """Test that is_daiv returns True when MR has 'daiv-max' label."""
        mr = MergeRequest(
            id=1,
            iid=1,
            title="Regular MR",
            labels=[Label(title="daiv-max")],
            state="open",
            source_branch="main",
            target_branch="feature",
        )
        assert mr.is_daiv() is True

    def test_mr_is_not_daiv_without_daiv_labels(self):
        """Test that is_daiv returns False when MR has no DAIV labels."""
        mr = MergeRequest(
            id=1,
            iid=1,
            title="Regular MR",
            labels=[Label(title="bug"), Label(title="feature")],
            state="open",
            source_branch="main",
            target_branch="feature",
        )
        assert mr.is_daiv() is False

    def test_mr_is_not_daiv_with_title_prefix(self):
        """Test that is_daiv returns False when MR title starts with 'daiv:' but has no label."""
        mr = MergeRequest(
            id=1,
            iid=1,
            title="daiv: Regular MR",
            labels=[Label(title="bug"), Label(title="feature")],
            state="open",
            source_branch="main",
            target_branch="feature",
        )
        assert mr.is_daiv() is False
