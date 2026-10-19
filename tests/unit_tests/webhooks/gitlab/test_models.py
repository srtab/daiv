from webhooks.gitlab.models import Issue, Label, MergeRequest


def test_issue_label_names_are_the_label_titles():
    issue = Issue(
        id=1,
        iid=1,
        title="t",
        description="",
        labels=[Label(title="bug"), Label(title="Daiv")],
        state="opened",
        type="Issue",
        assignee_id=None,
    )

    assert issue.label_names == ["bug", "Daiv"]


def test_merge_request_label_names_are_the_label_titles():
    merge_request = MergeRequest(
        id=1, iid=1, title="t", state="opened", source_branch="feat", target_branch="main", labels=[Label(title="daiv")]
    )

    assert merge_request.label_names == ["daiv"]
