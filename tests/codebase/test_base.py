from codebase.base import Issue, Job, MergeRequest, User


def test_is_daiv_with_bot_label():
    mr = MergeRequest(
        repo_id="123",
        merge_request_id=1,
        source_branch="feature",
        target_branch="main",
        title="Regular MR",
        description="Test MR",
        labels=["bug", "daiv", "feature"],
        sha="abc123",
    )
    assert mr.is_daiv() is True


def test_is_daiv_with_uppercase_bot_label():
    mr = MergeRequest(
        repo_id="123",
        merge_request_id=1,
        source_branch="feature",
        target_branch="main",
        title="Regular MR",
        description="Test MR",
        labels=["bug", "DAIV", "feature"],
        sha="abc123",
    )
    assert mr.is_daiv() is True


def test_is_daiv_with_bot_title():
    mr = MergeRequest(
        repo_id="123",
        merge_request_id=1,
        source_branch="feature",
        target_branch="main",
        title="daiv: Automated changes",
        description="Test MR",
        labels=[],
        sha="abc123",
    )
    assert mr.is_daiv() is True


def test_is_daiv_with_uppercase_bot_title():
    mr = MergeRequest(
        repo_id="123",
        merge_request_id=1,
        source_branch="feature",
        target_branch="main",
        title="DAIV: Automated changes",
        description="Test MR",
        labels=[],
        sha="abc123",
    )
    assert mr.is_daiv() is True


def test_is_not_daiv():
    mr = MergeRequest(
        repo_id="123",
        merge_request_id=1,
        source_branch="feature",
        target_branch="main",
        title="Regular MR",
        description="Test MR",
        labels=["bug", "feature"],
        sha="abc123",
    )
    assert mr.is_daiv() is False


def test_clean_title_normal_title():
    """Test that normal titles without bot label remain unchanged."""
    issue = Issue(title="Fix bug in authentication system", author=User(id=1, name="Test User", username="testuser"))
    assert issue.title == "Fix bug in authentication system"


def test_clean_title_with_bot_label():
    """Test that titles starting with 'daiv' have it removed."""
    issue = Issue(title="daiv Fix authentication bug", author=User(id=1, name="Test User", username="testuser"))
    assert issue.title == "Fix authentication bug"


def test_clean_title_with_bot_label_uppercase():
    """Test that titles starting with 'DAIV' have it removed (case insensitive)."""
    issue = Issue(title="DAIV Fix authentication bug", author=User(id=1, name="Test User", username="testuser"))
    assert issue.title == "Fix authentication bug"


def test_clean_title_with_bot_label_mixed_case():
    """Test that titles starting with mixed case 'DaIv' have it removed."""
    issue = Issue(title="DaIv Fix authentication bug", author=User(id=1, name="Test User", username="testuser"))
    assert issue.title == "Fix authentication bug"


def test_clean_title_with_bot_label_and_colon():
    """Test that titles starting with 'daiv:' have it removed."""
    issue = Issue(title="daiv: Fix authentication bug", author=User(id=1, name="Test User", username="testuser"))
    assert issue.title == "Fix authentication bug"


def test_clean_title_with_bot_label_and_colon_uppercase():
    """Test that titles starting with 'DAIV:' have it removed (case insensitive)."""
    issue = Issue(title="DAIV: Fix authentication bug", author=User(id=1, name="Test User", username="testuser"))
    assert issue.title == "Fix authentication bug"


def test_clean_title_with_bot_label_and_colon_mixed_case():
    """Test that titles starting with mixed case 'DaIv:' have it removed."""
    issue = Issue(title="DaIv: Fix authentication bug", author=User(id=1, name="Test User", username="testuser"))
    assert issue.title == "Fix authentication bug"


def test_clean_title_with_extra_whitespace():
    """Test that extra whitespace is properly stripped after removing bot label."""
    issue = Issue(title="daiv   Fix authentication bug", author=User(id=1, name="Test User", username="testuser"))
    assert issue.title == "Fix authentication bug"


def test_clean_title_with_colon_and_extra_whitespace():
    """Test that extra whitespace is properly stripped after removing bot label with colon."""
    issue = Issue(title="daiv:   Fix authentication bug", author=User(id=1, name="Test User", username="testuser"))
    assert issue.title == "Fix authentication bug"


def test_clean_title_bot_label_in_middle():
    """Test that bot label in the middle of title is not removed."""
    issue = Issue(title="Fix daiv authentication bug", author=User(id=1, name="Test User", username="testuser"))
    assert issue.title == "Fix daiv authentication bug"


def test_clean_title_empty_after_bot_label():
    """Test handling when only bot label is in the title."""
    issue = Issue(title="daiv", author=User(id=1, name="Test User", username="testuser"))
    assert issue.title == ""


def test_clean_title_empty_after_bot_label_with_colon():
    """Test handling when only bot label with colon is in the title."""
    issue = Issue(title="daiv:", author=User(id=1, name="Test User", username="testuser"))
    assert issue.title == ""


def test_clean_title_only_whitespace_after_bot_label():
    """Test handling when only whitespace follows bot label."""
    issue = Issue(title="daiv   ", author=User(id=1, name="Test User", username="testuser"))
    assert issue.title == ""


def test_clean_title_only_whitespace_after_bot_label_with_colon():
    """Test handling when only whitespace follows bot label with colon."""
    issue = Issue(title="daiv:   ", author=User(id=1, name="Test User", username="testuser"))
    assert issue.title == ""


def test_job_is_failed_when_status_is_failed():
    """Test that is_failed returns True when job status is 'failed'."""
    job = Job(id=1, name="test-job", status="failed", stage="test", allow_failure=False)
    assert job.is_failed() is True


def test_job_is_failed_when_status_is_not_failed():
    """Test that is_failed returns False when job status is not 'failed'."""
    job = Job(id=1, name="test-job", status="success", stage="test", allow_failure=False)
    assert job.is_failed() is False
