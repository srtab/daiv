import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from git import Repo

from codebase.base import GitPlatform, Repository
from evals import reviewbench
from evals.reviewbench import Location, Review, ReviewBenchRepoClient, parse_locations, report_findings, review_request
from tests.integration_tests.code_review_grading import parse_report

HEAD_FILES = frozenset({"src/engine.ts", "src/rules/index.ts", "lib/index.ts", "src/routes/[id]/page.ts", "README.md"})
ENTRY = {
    "repo": "https://github.com/owner/repo",
    "pr_number": 135,
    "base": "a" * 40,
    "head": "b" * 40,
    "nwo": "owner/repo",
    "title": "Guard against ReDoS",
    "body": "Caps pattern length.",
}
KEY = f"owner_repo_135-{'b' * 8}"
REPORT = """## Code Review

### Critical Issues
**1. Flags reach the RegExp unchecked** — [`src/engine.ts:42`](https://github.com/owner/repo/blob/bbbbbbb/src/engine.ts#L42)

<details>
<summary>Details</summary>

`flags` comes from the rules file, so an invalid flag throws at match time. Validate it on load.

</details>

### Suggestions
**1. Old guard removed** — `src/legacy.ts:10` (deleted)

<details>
<summary>Details</summary>

The removed guard was the only length check.

</details>

### Questions
**1. Is the 1000-character cap intended?** — `src/engine.ts:50`

### Recommended Actions
1. Validate the flags on load.
"""
CLOSING_PROSE = """
### Suggestions
**1. Duplicated flag allowlist** — `src/engine.ts:30`

<details>
<summary>Details</summary>

The allowlist is repeated in two methods.

</details>

---

No other defects found. A minor design note: the cache is never cleared.
"""


def _commit(repo: Repo, content: str) -> str:
    Path(repo.working_dir, "a.txt").write_text(content)
    repo.index.add(["a.txt"])
    return repo.index.commit(content).hexsha


REPOSITORY = Repository(
    pk=1, slug="owner/repo", name="repo", clone_url="", html_url="", default_branch="main",
    git_platform=GitPlatform.SWE, topics=[],
)  # fmt: skip


def test_the_checkout_is_the_heads_tree_with_base_present_and_nothing_naming_the_mirror(tmp_path):
    mirror = Repo.init(tmp_path / "mirror")
    base = _commit(mirror, "base")
    head = _commit(mirror, "head")
    _commit(mirror, "a later fix")

    with ReviewBenchRepoClient((tmp_path / "mirror").as_uri(), base).load_repo(REPOSITORY, head) as repo:
        assert repo.head.is_detached
        assert repo.head.commit.hexsha == head
        assert "+head" in repo.git.diff(f"{base}...{head}")
        assert (repo.branches, repo.remotes, repo.tags) == ([], [], [])
        assert not (Path(repo.git_dir) / "FETCH_HEAD").exists()


def test_a_merge_base_beyond_the_first_fetch_depth_is_fetched_by_deepening(tmp_path):
    mirror = Repo.init(tmp_path / "mirror")
    _commit(mirror, "root")
    fork = mirror.head.commit
    for content in ("main 1", "main 2", "main 3"):
        base = _commit(mirror, content)
    mirror.git.checkout("-q", "-b", "topic", fork.hexsha)
    head = _commit(mirror, "head")

    with (
        patch.object(reviewbench, "FETCH_DEPTH", 1),
        ReviewBenchRepoClient((tmp_path / "mirror").as_uri(), base).load_repo(REPOSITORY, head) as repo,
    ):
        assert repo.git.merge_base(base, head) == fork.hexsha
        assert "+head" in repo.git.diff(f"{base}...{head}")


def test_the_review_request_names_the_three_dot_range_and_the_pull_request():
    request = review_request(ENTRY)

    assert request.startswith("/code-review ")
    assert f"git diff {'a' * 40}...{'b' * 40}" in request
    assert "Guard against ReDoS" in request
    assert request.endswith("## Pull request description\n\nCaps pattern length.")


def test_a_pull_request_without_a_description_asks_for_the_range_alone():
    assert "Pull request description" not in review_request(ENTRY | {"body": None})


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        pytest.param(
            "[`src/engine.ts:42`](https://github.com/o/r/blob/abc1234/src/engine.ts#L42)",
            Location("src/engine.ts", 42, 42),
            id="link-text",
        ),
        pytest.param("`src/engine.ts:45-42`", Location("src/engine.ts", 42, 45), id="reversed-range"),
        pytest.param(
            "[`engine`](https://github.com/o/r/blob/abc1234/src/engine.ts#L7-L9)",
            Location("src/engine.ts", 7, 9),
            id="blob-url-range",
        ),
        pytest.param("`/workspace/repo/lib/index.ts:3`", Location("lib/index.ts", 3, 3), id="sandbox-path"),
        pytest.param("`rules/index.ts:5`", Location("src/rules/index.ts", 5, 5), id="unique-suffix"),
        pytest.param("`src/routes/[id]/page.ts:4`", Location("src/routes/[id]/page.ts", 4, 4), id="route-param-dir"),
    ],
)
def test_a_location_resolves_to_a_tracked_head_file_and_its_lines(location, expected):
    assert parse_locations(location, HEAD_FILES) == [expected]


def test_a_location_line_naming_several_files_resolves_to_the_first_line_in_each():
    location = "`src/engine.ts:42` (also `src/engine.ts:50`, `lib/index.ts:7` and `src/legacy.ts:3` (deleted))"

    assert parse_locations(location, HEAD_FILES) == [Location("src/engine.ts", 42, 42), Location("lib/index.ts", 7, 7)]


@pytest.mark.parametrize(
    "location",
    [
        pytest.param("`src/engine.ts:42` (deleted)", id="deleted-side"),
        pytest.param("`index.ts:3`", id="ambiguous-suffix"),
        pytest.param("`src/missing.ts:3`", id="untracked"),
        pytest.param("`src/engine.ts`", id="no-line"),
    ],
)
def test_a_location_without_one_head_side_line_in_a_tracked_file_is_unlocated(location):
    assert parse_locations(location, HEAD_FILES) == []


def test_reported_findings_become_judging_findings_and_questions_are_left_out():
    findings, unlocated = report_findings(parse_report(REPORT), HEAD_FILES)

    assert findings == [
        {
            "file": "src/engine.ts",
            "start_line": 42,
            "end_line": 42,
            "message": "Flags reach the RegExp unchecked\n\n`flags` comes from the rules file, so an invalid flag "
            "throws at match time. Validate it on load.",
            "producer": "daiv",
        }
    ]
    assert [finding.title for finding in unlocated] == ["Old guard removed"]


def test_prose_after_a_thematic_break_is_not_part_of_the_last_finding():
    [finding], _ = report_findings(parse_report(CLOSING_PROSE), HEAD_FILES)

    assert finding["message"] == "Duplicated flag allowlist\n\nThe allowlist is repeated in two methods."


def test_a_finding_whose_location_line_names_no_line_takes_the_first_its_details_name():
    report = """### Suggestions

**1. Unrelated edits.** `AGENTS.md` says to stay in scope.
- `lib/index.ts:12` changes `save()` to `void save()`.
- `src/engine.ts:41` widens a regex.
"""
    [finding], unlocated = report_findings(parse_report(report), HEAD_FILES)

    assert (finding["file"], finding["start_line"], unlocated) == ("lib/index.ts", 12, [])


def test_a_one_line_entry_takes_the_first_line_its_title_names():
    report = "### Suggestions\n\n**3.** `lib/index.ts:4-5` says the cache is cleared; `src/engine.ts:9` never is.\n"

    [finding], unlocated = report_findings(parse_report(report), HEAD_FILES)

    assert (finding["file"], finding["start_line"], finding["end_line"], unlocated) == ("lib/index.ts", 4, 5, [])


def test_a_finding_on_several_files_becomes_one_judging_finding_per_file():
    report = (
        "### Important Issues\n**1. Raw origin used** — `src/engine.ts:28`, `lib/index.ts:42`\n\nUse the public origin."
    )

    findings, _ = report_findings(parse_report(report), HEAD_FILES)

    assert [(finding["file"], finding["start_line"]) for finding in findings] == [
        ("src/engine.ts", 28),
        ("lib/index.ts", 42),
    ]
    assert len({finding["message"] for finding in findings}) == 1


@pytest.fixture
def reviewbench_dir(tmp_path) -> Path:
    corpus = tmp_path / "rb" / "corpus" / "test"
    corpus.mkdir(parents=True)
    (corpus / "test.json").write_text(json.dumps([ENTRY]))
    return tmp_path / "rb"


def _patched_main(report: str):
    outcome = Review(report=report, head_files=HEAD_FILES, usage={"time_in_ms": 5})
    review = AsyncMock(return_value=outcome)
    patches = (
        patch.object(reviewbench, "validate_agent_override", MagicMock()),
        patch.object(reviewbench, "site_settings", MagicMock(sandbox_api_key="key")),
        patch.object(reviewbench, "review", review),
    )
    return review, patches


async def test_a_reviewed_pull_request_writes_a_judging_input_file_and_a_rerun_skips_it(reviewbench_dir, tmp_path):
    review, (validate, settings, reviewing) = _patched_main(REPORT)
    with validate, settings, reviewing:
        await reviewbench.main(reviewbench_dir, "test", tmp_path / "out", "model-a")
        await reviewbench.main(reviewbench_dir, "test", tmp_path / "out", "model-a")

    assert review.await_count == 1
    written = json.loads((tmp_path / "out" / "findings" / f"{KEY}.json").read_text())
    assert written["pr"] == {"repo": ENTRY["repo"], "pr_number": 135, "base": ENTRY["base"], "head": ENTRY["head"]}
    assert written["agent"] == "daiv/model-a"
    assert [finding["file"] for finding in written["findings"]] == ["src/engine.ts"]
    assert written["usage"] == {"time_in_ms": 5}
    assert (tmp_path / "out" / "reports" / f"{KEY}.md").read_text() == REPORT


async def test_a_final_message_that_is_no_review_fails_the_pull_request_instead_of_scoring_it_empty(
    reviewbench_dir, tmp_path
):
    _, (validate, settings, reviewing) = _patched_main("Which branch should I review?")
    with validate, settings, reviewing, pytest.raises(SystemExit, match="1 pull request"):
        await reviewbench.main(reviewbench_dir, "test", tmp_path / "out", "model-a")

    assert not list((tmp_path / "out" / "findings").iterdir())
