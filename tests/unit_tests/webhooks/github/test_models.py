from webhooks.github.models import Issue, Label


def test_issue_label_names_are_the_label_names():
    issue = Issue(id=1, number=1, title="t", state="open", labels=[Label(id=1, name="bug"), Label(id=2, name="Daiv")])

    assert issue.label_names == ["bug", "Daiv"]
