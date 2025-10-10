from codebase.clients.gitlab.api.models import Issue, Label, MergeRequest


def test_issue_is_daiv_with_bot_label():
    issue = Issue(
        id=1,
        iid=1,
        title="Regular MR",
        description="Test MR",
        labels=[Label(title="bug"), Label(title="daiv"), Label(title="feature")],
        state="open",
        type="Issue",
        assignee_id=1,
    )
    assert issue.is_daiv() is True


def test_issue_is_daiv_with_bot_title():
    issue = Issue(
        id=1,
        iid=1,
        title="daiv: Regular MR",
        description="Test MR",
        labels=[Label(title="bug"), Label(title="feature")],
        state="open",
        type="Issue",
        assignee_id=1,
    )
    assert issue.is_daiv() is True


def test_issue_is_not_daiv():
    issue = Issue(
        id=1,
        iid=1,
        title="Regular MR",
        description="Test MR",
        labels=[Label(title="bug"), Label(title="feature")],
        state="open",
        type="Issue",
        assignee_id=1,
    )
    assert issue.is_daiv() is False


def test_mr_is_daiv_with_bot_label():
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


def test_mr_is_daiv_with_bot_title():
    mr = MergeRequest(
        id=1,
        iid=1,
        title="daiv: Regular MR",
        labels=[Label(title="bug"), Label(title="feature")],
        state="open",
        source_branch="main",
        target_branch="feature",
    )
    assert mr.is_daiv() is True


def test_mr_is_not_daiv():
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
