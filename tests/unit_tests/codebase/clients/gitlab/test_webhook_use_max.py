from codebase.clients.gitlab.api.models import Issue, Label, MergeRequest


def _label(title: str) -> Label:
    return Label(title=title)


def _issue(labels: list[Label]) -> Issue:
    return Issue(id=1, iid=1, title="t", description="", state="opened", assignee_id=None, labels=labels, type="Issue")


def _merge_request(labels: list[Label]) -> MergeRequest:
    return MergeRequest(
        id=1, iid=1, title="t", state="opened", source_branch="feature", target_branch="main", labels=labels
    )


def test_issue_has_max_label_true():
    assert _issue([_label("daiv-max")]).has_max_label() is True


def test_issue_has_max_label_case_insensitive():
    assert _issue([_label("DAIV-Max")]).has_max_label() is True


def test_issue_has_max_label_false_when_absent():
    assert _issue([_label("daiv")]).has_max_label() is False


def test_merge_request_has_max_label_true():
    assert _merge_request([_label("daiv-max")]).has_max_label() is True


def test_merge_request_has_max_label_false_when_absent():
    assert _merge_request([_label("daiv")]).has_max_label() is False
