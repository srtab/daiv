from codebase.clients.github.api.models import Issue, Label, PullRequest, Ref


def _label(name: str) -> Label:
    return Label(id=1, name=name)


def _issue(labels: list[Label]) -> Issue:
    return Issue(id=1, number=1, title="t", state="open", labels=labels)


def _pull_request(labels: list[Label]) -> PullRequest:
    return PullRequest(
        id=1,
        number=1,
        title="t",
        state="open",
        head=Ref(ref="feature", sha="abc"),
        base=Ref(ref="main", sha="def"),
        labels=labels,
    )


def test_issue_has_max_label_true():
    assert _issue([_label("daiv-max")]).has_max_label() is True


def test_issue_has_max_label_case_insensitive():
    assert _issue([_label("DAIV-MAX")]).has_max_label() is True


def test_issue_has_max_label_false_when_absent():
    assert _issue([_label("daiv")]).has_max_label() is False


def test_pull_request_has_max_label_true():
    assert _pull_request([_label("daiv-max")]).has_max_label() is True


def test_pull_request_has_max_label_false_when_absent():
    assert _pull_request([_label("daiv")]).has_max_label() is False
