from codebase.base import Job, RepoAccessLevel, RepoMember


def test_job_is_failed_when_status_is_failed():
    """Test that is_failed returns True when job status is 'failed'."""
    job = Job(id=1, name="test-job", status="failed", stage="test", allow_failure=False)
    assert job.is_failed() is True


def test_job_is_failed_when_status_is_not_failed():
    """Test that is_failed returns False when job status is not 'failed'."""
    job = Job(id=1, name="test-job", status="success", stage="test", allow_failure=False)
    assert job.is_failed() is False


def test_repo_member_model():
    member = RepoMember(uid="42", username="alice", access_level=RepoAccessLevel.WRITE)
    assert member.uid == "42"
    assert member.access_level == "write"
