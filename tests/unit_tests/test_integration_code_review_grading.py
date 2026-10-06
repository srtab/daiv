"""Coverage for the recall suite's grading: case validation, report parsing, the clean-twin rule and bug bookkeeping.

The judge call itself is stubbed here; the ``code_review`` integration suite exercises it.
"""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.integration_tests import code_review_grading
from tests.integration_tests.code_review_grading import (
    Finding,
    JudgeError,
    blocking,
    clean_case_violation,
    grade_bug_case,
    is_degraded,
    is_review_report,
    judge_planted_bug,
    located_in,
    location_paths,
    parse_report,
    severity_counts,
    validate_cases,
)
from tests.integration_tests.evaluators import Verdict

SHA = "f5580ea4d7011f7e005fa46bfc0f3ba935595f69"
DATA_DIR = Path(__file__).parents[1] / "integration_tests" / "data" / "code_review"
PATCH = (
    "diff --git a/daiv/app/views.py b/daiv/app/views.py\n"
    "--- a/daiv/app/views.py\n+++ b/daiv/app/views.py\n@@ -1 +1 @@\n-a\n+b\n"
)


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "patches").mkdir()
    for name in ("bug", "clean"):
        (tmp_path / "patches" / f"{name}.patch").write_text(PATCH)
    return tmp_path


def bug_case(**overrides) -> dict:
    planted = {
        "file": "daiv/app/views.py",
        "lines": [1, 1],
        "defect": "The view skips the owner check.",
        "dimension": "security",
    }
    return {
        "id": "bug",
        "base_sha": SHA,
        "patch_path": "patches/bug.patch",
        "kind": "bug",
        "planted": planted,
    } | overrides


def clean_case(**overrides) -> dict:
    return {
        "id": "clean",
        "base_sha": SHA,
        "patch_path": "patches/clean.patch",
        "kind": "clean",
        "twin": "bug",
    } | overrides


class TestValidateCases:
    def test_a_bug_case_and_its_clean_twin_pass(self, data_dir):
        validate_cases([bug_case(), clean_case()], data_dir)

    @pytest.mark.parametrize(
        "case,message",
        [
            pytest.param(bug_case(extra=1), "unknown key", id="unknown-key"),
            pytest.param(bug_case(base_sha="main"), "base_sha", id="ref-not-sha"),
            pytest.param(bug_case(patch_path="patches/missing.patch"), "no patch file", id="missing-patch"),
            pytest.param(bug_case(patch_path="../../etc/passwd.patch"), "no patch file", id="patch-outside-data"),
            pytest.param(bug_case(kind="flaky"), "kind must be", id="unknown-kind"),
            pytest.param(bug_case(planted=None), "planted needs", id="bug-without-plant"),
            pytest.param(bug_case(twin="clean"), "only a clean case", id="bug-with-twin"),
        ],
    )
    def test_a_malformed_bug_case_is_rejected(self, data_dir, case, message):
        with pytest.raises(ValueError, match=message):
            validate_cases([case], data_dir)

    @pytest.mark.parametrize(
        "planted,message",
        [
            pytest.param({"file": "daiv/other.py"}, "does not change planted.file", id="file-not-in-patch"),
            pytest.param({"lines": [5, 2]}, "planted.lines", id="lines-reversed"),
            pytest.param({"lines": [0, 1]}, "planted.lines", id="line-zero"),
            pytest.param({"defect": " "}, "planted.defect", id="empty-defect"),
            pytest.param({"dimension": "style"}, "planted.dimension", id="unknown-dimension"),
        ],
    )
    def test_a_malformed_plant_is_rejected(self, data_dir, planted, message):
        case = bug_case()
        case["planted"] = case["planted"] | planted

        with pytest.raises(ValueError, match=message):
            validate_cases([case], data_dir)

    def test_a_clean_twin_must_name_a_bug_case(self, data_dir):
        with pytest.raises(ValueError, match="twin must name a bug case"):
            validate_cases([bug_case(), clean_case(twin="nope")], data_dir)

    def test_a_clean_twin_shares_its_bug_base(self, data_dir):
        with pytest.raises(ValueError, match="share its bug case's base_sha"):
            validate_cases([bug_case(), clean_case(base_sha="0" * 40)], data_dir)

    def test_a_clean_case_plants_nothing(self, data_dir):
        with pytest.raises(ValueError, match="plants nothing"):
            validate_cases([bug_case(), clean_case(planted=bug_case()["planted"])], data_dir)

    def test_duplicate_ids_are_rejected(self, data_dir):
        with pytest.raises(ValueError, match="duplicate"):
            validate_cases([bug_case(), bug_case()], data_dir)


REPORT = """\
## Code Review

### Critical Issues
**1. Export skips the owner check** — [`daiv/schedules/views.py:410`](https://git.example.com/srtab/daiv/-/blob/abc/daiv/schedules/views.py#L410)

<details>
<summary>Details</summary>

Any logged-in user can download another user's schedule.
1. Add `_ScheduleOwnerMixin`.

</details>

### Important Issues
**1. Prompt is exported verbatim** — `daiv/schedules/views.py:414`

<details>
<summary>Details</summary>

Prompts may hold secrets.

</details>

### Suggestions
**1. Name the download after the schedule** — views.py:420

### Questions
**1. Should subscribers be exported?** — `daiv/schedules/views.py:421`

### Recommended Actions
1. Scope the export to the owner.
"""


class TestParseReport:
    def test_reads_each_section_entry_title_and_location(self):
        findings = parse_report(REPORT)

        assert [(f.severity, f.title) for f in findings] == [
            ("Critical", "Export skips the owner check"),
            ("Important", "Prompt is exported verbatim"),
            ("Suggestion", "Name the download after the schedule"),
            ("Question", "Should subscribers be exported?"),
        ]
        assert "Any logged-in user" in findings[0].details
        assert "<details>" not in findings[0].details

    def test_a_numbered_line_inside_details_is_not_a_new_finding(self):
        assert len([f for f in parse_report(REPORT) if f.severity == "Critical"]) == 1

    def test_recommended_actions_are_not_findings(self):
        assert not any("Scope the export" in f.title for f in parse_report(REPORT))

    def test_short_section_headings(self):
        report = "## Code Review\n\n### Critical:\n1. **Missing await** — `daiv/mcp_api/server.py:524`\n"

        [finding] = parse_report(report)

        assert (finding.severity, finding.title) == ("Critical", "Missing await")
        assert finding.paths == ("daiv/mcp_api/server.py",)

    def test_a_severity_word_after_an_emoji(self):
        report = "## Code Review\n\n### 🔴 Critical Issues\n**1. Missing await** — `daiv/mcp_api/server.py:524`\n"

        [finding] = parse_report(report)

        assert (finding.severity, finding.title) == ("Critical", "Missing await")

    def test_a_location_given_only_inside_the_details(self):
        report = "### Important Issues\n**1. Wrong default**\n\n- **Location:** `daiv/sessions/managers.py:34`\n"

        assert parse_report(report)[0].paths == ("daiv/sessions/managers.py",)

    def test_no_findings(self):
        assert parse_report("## Code Review\n\nNo findings — no reported issues met the review threshold.") == []


class TestLocatedIn:
    @pytest.mark.parametrize(
        "location",
        [
            pytest.param("`daiv/jobs/api/views.py:141`", id="repo-relative"),
            pytest.param("`/workspace/repo/daiv/jobs/api/views.py:141`", id="absolute-sandbox-path"),
            pytest.param(
                "[`views.py:141`](https://h/srtab/daiv/-/blob/abc/daiv/jobs/api/views.py#L141)", id="blob-url"
            ),
            pytest.param("views.py:141", id="basename"),
            pytest.param("`daiv/jobs/api/views.py:141 (deleted)`", id="deleted-side"),
        ],
    )
    def test_every_way_a_report_names_the_planted_file_reaches_the_judge(self, location):
        finding = Finding(severity="Important", title="t", location=location, details="")

        assert located_in(finding, "daiv/jobs/api/views.py")

    def test_another_file_does_not(self):
        finding = Finding(severity="Critical", title="t", location="`daiv/jobs/api/schemas.py:20`", details="")

        assert not located_in(finding, "daiv/jobs/api/views.py")

    def test_a_question_is_not_a_finding(self):
        finding = Finding(severity="Question", title="t", location="`daiv/jobs/api/views.py:1`", details="")

        assert not located_in(finding, "daiv/jobs/api/views.py")

    def test_a_non_blob_url_contributes_no_path(self):
        assert location_paths("see https://docs.example.com/guide.html") == ()


class TestCleanCase:
    def test_suggestions_and_questions_only_pass(self):
        report = (
            "## Code Review\n\n### Suggestions\n**1. Rename** — `a.py:1`\n\n### Questions\n**1. Why?** — `a.py:2`\n"
        )

        assert clean_case_violation(report, parse_report(report)) is None

    def test_an_important_finding_fails(self):
        report = "## Code Review\n\n### Important Issues\n**1. Leak** — `a.py:1`\n"

        assert "1 Critical/Important" in clean_case_violation(report, parse_report(report))

    def test_a_run_that_ends_without_a_report_fails(self):
        report = "I ran out of steps before the detectors finished."

        assert "did not end on a code-review report" in clean_case_violation(report, parse_report(report))

    def test_no_findings_mid_sentence_fails(self):
        report = "I ran out of steps. No findings were confirmed yet."

        assert not is_review_report(report)
        assert "did not end on a code-review report" in clean_case_violation(report, parse_report(report))

    def test_no_findings_passes(self):
        report = "## Code Review\n\nNo findings — no reported issues met the review threshold."

        assert is_review_report(report)
        assert clean_case_violation(report, []) is None

    def test_a_heading_less_no_findings_line_passes(self):
        report = "No findings — no reported issues met the review threshold."

        assert is_review_report(report)
        assert clean_case_violation(report, parse_report(report)) is None

    def test_a_degraded_review_fails(self):
        report = (
            "## Code Review\n\n_Review unavailable for: security._\n\n"
            "No findings — none confirmed by the detectors that completed.\n"
        )

        assert is_degraded(report)
        assert "degraded" in clean_case_violation(report, parse_report(report))

    def test_a_review_that_could_not_be_completed_fails(self):
        report = "## Code Review\n\nThe review could not be completed: no changed-hunk diff could be obtained.\n"

        assert "no parsed finding" in clean_case_violation(report, parse_report(report))

    @pytest.mark.parametrize(
        "entries",
        [
            pytest.param("### Important Issues\n- **Leak** — `a.py:1`\n", id="bulleted"),
            pytest.param("### Important Issues\n\n#### Leak\n`a.py:1` keeps the handle open.\n", id="h4-entries"),
        ],
    )
    def test_entries_the_parser_cannot_read_fail(self, entries):
        report = f"## Code Review\n\n{entries}"

        assert parse_report(report) == []
        assert "no parsed finding" in clean_case_violation(report, [])

    def test_a_complete_review_is_not_degraded(self):
        assert not is_degraded(REPORT)


class TestGradeBugCase:
    @pytest.fixture
    def judged(self, monkeypatch) -> list[str]:
        """The titles the judge saw; it matches only the finding titled "Unscoped lookup"."""
        calls: list[str] = []

        async def judge(defect: str, finding: Finding) -> Verdict:
            calls.append(finding.title)
            return Verdict(passed=finding.title == "Unscoped lookup", explanation=finding.title)

        monkeypatch.setattr(code_review_grading, "judge_planted_bug", judge)
        return calls

    async def test_the_hit_is_the_only_blocking_finding_left_out_of_the_noise(self, judged):
        findings = [
            Finding(severity="Critical", title="Missing throttle", location="`daiv/app/views.py:3`", details=""),
            Finding(severity="Important", title="Unscoped lookup", location="`daiv/app/views.py:1`", details=""),
            Finding(severity="Important", title="Route not listed", location="`daiv/daiv/api.py:9`", details=""),
            Finding(severity="Suggestion", title="Name the route", location="`daiv/app/views.py:2`", details=""),
            Finding(severity="Question", title="Admins too?", location="`daiv/app/views.py:1`", details=""),
        ]

        grade = await grade_bug_case(findings, bug_case()["planted"])

        assert judged == ["Missing throttle", "Unscoped lookup", "Name the route"]
        assert (grade.hit, grade.hit_severity, grade.noise) == (True, "Important", 2)

    async def test_a_miss_has_no_hit_severity(self, judged):
        finding = Finding(severity="Critical", title="Missing throttle", location="`daiv/app/views.py:3`", details="")

        grade = await grade_bug_case([finding], bug_case()["planted"])

        assert (grade.hit, grade.hit_severity, grade.noise) == (False, None, 1)


class TestJudgePlantedBug:
    FINDING = Finding(severity="Critical", title="Unscoped lookup", location="`daiv/app/views.py:1`", details="")

    def _judge_answers(self, monkeypatch, answer) -> None:
        async def ainvoke(prompt: str):
            if isinstance(answer, Exception):
                raise answer
            return answer

        structured = SimpleNamespace(ainvoke=ainvoke)
        monkeypatch.setattr(
            code_review_grading, "_judge", lambda: SimpleNamespace(with_structured_output=lambda schema: structured)
        )

    async def test_returns_the_verdict(self, monkeypatch):
        self._judge_answers(monkeypatch, Verdict(passed=True, explanation="same defect"))

        assert (await judge_planted_bug("unscoped", self.FINDING)).passed

    async def test_an_empty_answer_is_a_judge_fault_not_a_miss(self, monkeypatch):
        self._judge_answers(monkeypatch, None)

        with pytest.raises(JudgeError, match="returned nothing"):
            await judge_planted_bug("unscoped", self.FINDING)

    async def test_a_provider_error_is_a_judge_fault_not_a_miss(self, monkeypatch):
        self._judge_answers(monkeypatch, RuntimeError("502 Bad Gateway"))

        with pytest.raises(JudgeError, match="502 Bad Gateway"):
            await judge_planted_bug("unscoped", self.FINDING)


def test_severity_counts_and_blocking():
    findings = parse_report(REPORT)

    assert severity_counts(findings) == {"Critical": 1, "Important": 1, "Suggestion": 1, "Question": 1}
    assert [f.severity for f in blocking(findings)] == ["Critical", "Important"]


class TestShippedCases:
    def test_the_shipped_cases_pass(self):
        validate_cases(_shipped_cases(), DATA_DIR)

    def test_they_pair_every_dimension_with_a_clean_twin(self):
        cases = _shipped_cases()
        twinned = {case["twin"] for case in cases if case["kind"] == "clean"}
        dimensions = {case["planted"]["dimension"] for case in cases if case["id"] in twinned}

        assert dimensions == {"correctness", "security", "performance"}

    def test_a_missing_patch_fails_validation(self, tmp_path):
        shutil.copytree(DATA_DIR, tmp_path / "code_review")
        (tmp_path / "code_review" / "patches" / "clean-mute-job.patch").unlink()

        with pytest.raises(ValueError, match="clean-mute-job"):
            validate_cases(_shipped_cases(), tmp_path / "code_review")


def _shipped_cases() -> list[dict]:
    return [json.loads(line) for line in (DATA_DIR / "cases.jsonl").read_text().splitlines() if line.strip()]
