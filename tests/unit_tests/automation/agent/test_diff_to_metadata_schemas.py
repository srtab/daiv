import pytest

from automation.agent.diff_to_metadata.schemas import DEFAULT_BRANCH_NAME, PullRequestMetadata, normalize_branch_name


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Well-formed names pass through untouched (dots are git-legal).
        ("chore/release-1.0.0", "chore/release-1.0.0"),  # the exact Sentry DAIV-AV input
        ("feat/add-export", "feat/add-export"),
        # Casing and disallowed characters are normalized.
        ("Feature/Add-Export", "feature/add-export"),
        ("feat/add export", "feat/add-export"),
        ("feat/fix~^:?*bug", "feat/fix-bug"),
        ("fix\\bug", "fix-bug"),
        ("Feat: Big Change!", "feat-big-change"),
        # git ref-format guards.
        ("-feat/x-", "feat/x"),
        ("/feat/x/", "feat/x"),
        ("feat..x", "feat.x"),
        ("feat//x", "feat/x"),
        ("feat/x.lock", "feat/x"),
        # Degenerate input falls back to a valid default.
        ("", DEFAULT_BRANCH_NAME),
        ("   ", DEFAULT_BRANCH_NAME),
        ("!!!", DEFAULT_BRANCH_NAME),
        ("..", DEFAULT_BRANCH_NAME),
    ],
)
def test_normalize_branch_name(value: str, expected: str) -> None:
    assert normalize_branch_name(value) == expected


def test_pull_request_metadata_accepts_dotted_version_branch() -> None:
    """The Sentry DAIV-AV input must parse without raising and be kept verbatim."""
    metadata = PullRequestMetadata(branch="chore/release-1.0.0", title="Release 1.0.0", description="desc")
    assert metadata.branch == "chore/release-1.0.0"


def test_pull_request_metadata_normalizes_invalid_branch_instead_of_raising() -> None:
    metadata = PullRequestMetadata(branch="Feat: Big Change!", title="t", description="d")
    assert metadata.branch == "feat-big-change"


def test_pull_request_metadata_falls_back_on_empty_branch() -> None:
    metadata = PullRequestMetadata(branch="", title="t", description="d")
    assert metadata.branch == DEFAULT_BRANCH_NAME
