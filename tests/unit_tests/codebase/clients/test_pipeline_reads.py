import pytest

from codebase.clients.github.client import github_conclusion_to_status


@pytest.mark.parametrize(
    ("conclusion", "expected"),
    [
        ("success", "success"),
        ("neutral", "success"),
        ("failure", "failed"),
        ("timed_out", "failed"),
        ("cancelled", "canceled"),
        ("stale", "canceled"),
        ("skipped", "skipped"),
        ("action_required", "manual"),
    ],
)
def test_github_conclusion_maps_to_domain_status(conclusion, expected):
    assert github_conclusion_to_status(conclusion) == expected


def test_an_unfinished_github_run_is_running():
    assert github_conclusion_to_status(None) == "running"


def test_an_unknown_conclusion_is_not_silently_green():
    # A vocabulary GitHub adds later must not be read as success.
    assert github_conclusion_to_status("something_new") == "canceled"
