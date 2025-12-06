from codebase.base import Issue, Job, User


def test_job_is_failed_when_status_is_failed():
    """Test that is_failed returns True when job status is 'failed'."""
    job = Job(id=1, name="test-job", status="failed", stage="test", allow_failure=False)
    assert job.is_failed() is True


def test_job_is_failed_when_status_is_not_failed():
    """Test that is_failed returns False when job status is not 'failed'."""
    job = Job(id=1, name="test-job", status="success", stage="test", allow_failure=False)
    assert job.is_failed() is False


class TestIssueHasAutoLabel:
    """Test the has_auto_label() method for Issue model."""

    def test_has_auto_label_returns_true_with_daiv_auto_label(self):
        """Test that has_auto_label returns True when issue has 'daiv-auto' label."""
        issue = Issue(
            id=1,
            iid=1,
            title="Test Issue",
            author=User(id=1, username="user", name="User"),
            labels=["bug", "daiv-auto", "feature"],
        )
        assert issue.has_auto_label() is True

    def test_has_auto_label_returns_true_case_insensitive(self):
        """Test that has_auto_label is case-insensitive."""
        issue = Issue(
            id=1, iid=1, title="Test Issue", author=User(id=1, username="user", name="User"), labels=["DAIV-AUTO"]
        )
        assert issue.has_auto_label() is True

    def test_has_auto_label_returns_false_without_auto_label(self):
        """Test that has_auto_label returns False when issue doesn't have 'daiv-auto' label."""
        issue = Issue(
            id=1,
            iid=1,
            title="Test Issue",
            author=User(id=1, username="user", name="User"),
            labels=["bug", "daiv", "feature"],
        )
        assert issue.has_auto_label() is False

    def test_has_auto_label_returns_false_with_empty_labels(self):
        """Test that has_auto_label returns False when issue has no labels."""
        issue = Issue(id=1, iid=1, title="Test Issue", author=User(id=1, username="user", name="User"), labels=[])
        assert issue.has_auto_label() is False


class TestIssueHasMaxLabel:
    """Test the has_max_label() method for Issue model."""

    def test_has_max_label_returns_true_with_daiv_max_label(self):
        """Test that has_max_label returns True when issue has 'daiv-max' label."""
        issue = Issue(
            id=1,
            iid=1,
            title="Test Issue",
            author=User(id=1, username="user", name="User"),
            labels=["bug", "daiv-max", "feature"],
        )
        assert issue.has_max_label() is True

    def test_has_max_label_returns_true_case_insensitive(self):
        """Test that has_max_label is case-insensitive."""
        issue = Issue(
            id=1, iid=1, title="Test Issue", author=User(id=1, username="user", name="User"), labels=["DAIV-MAX"]
        )
        assert issue.has_max_label() is True

    def test_has_max_label_returns_false_without_max_label(self):
        """Test that has_max_label returns False when issue doesn't have 'daiv-max' label."""
        issue = Issue(
            id=1,
            iid=1,
            title="Test Issue",
            author=User(id=1, username="user", name="User"),
            labels=["bug", "daiv", "feature"],
        )
        assert issue.has_max_label() is False

    def test_has_max_label_returns_false_with_empty_labels(self):
        """Test that has_max_label returns False when issue has no labels."""
        issue = Issue(id=1, iid=1, title="Test Issue", author=User(id=1, username="user", name="User"), labels=[])
        assert issue.has_max_label() is False
