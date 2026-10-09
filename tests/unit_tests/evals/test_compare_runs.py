import json

import pytest

from evals.compare_runs import Votes, compare, main, recall_summary, render_markdown, suite_medians

SUITE = "DAIV: Skills"


def rows(
    nodeid: str, outcomes: list[bool | None], *, suite: str = SUITE, input_tokens: int = 1000, **extra
) -> list[dict]:
    return [
        {
            "nodeid": nodeid,
            "suite": suite,
            "case": nodeid.rsplit("::", 1)[-1],
            "model": "openrouter:z-ai/glm-5.2",
            "run": run,
            "git_sha": "abc123",
            "passed": passed,
            "input_tokens": input_tokens,
            "output_tokens": 100,
            "cache_read_tokens": 0,
            "turns": 2,
            "cost": None,
            **extra,
        }
        for run, passed in enumerate(outcomes, start=1)
    ]


class TestVotes:
    def test_majority_needs_more_than_half(self):
        assert Votes(2, 3).majority
        assert not Votes(1, 3).majority
        assert not Votes(1, 2).majority

    def test_unanimous_needs_three_agreeing_votes(self):
        assert Votes(0, 3).unanimous
        assert Votes(3, 3).unanimous
        assert not Votes(2, 3).unanimous
        assert not Votes(1, 1).unanimous


class TestVerdict:
    def test_a_file_compared_with_itself_is_neutral(self):
        data = rows("t::a", [True, True, False]) + rows("t::b", [False, False, False])

        assert compare(data, data).verdict == "neutral"

    def test_two_of_three_to_three_of_three_is_not_a_gain(self):
        assert compare(rows("t::a", [True, True, False]), rows("t::a", [True] * 3)).verdict == "neutral"

    def test_an_unstable_fail_to_pass_flip_is_not_a_gain_and_needs_no_confirmation(self):
        comparison = compare(rows("t::a", [False, False, True]), rows("t::a", [True, True, False]))

        assert comparison.verdict == "neutral"
        assert comparison.deltas[0].flip == "FAIL→PASS (unstable, not a gain)"

    def test_fewer_input_tokens_alone_is_not_an_improvement(self):
        before = rows("t::a", [True] * 3, input_tokens=1000)
        after = rows("t::a", [True] * 3, input_tokens=500)

        assert compare(before, after).verdict == "neutral"


class TestConfirmation:
    def test_a_pass_to_fail_flip_waits_for_confirmation(self):
        comparison = compare(rows("t::a", [True] * 3), rows("t::a", [True, False, False]))

        assert comparison.verdict == "unconfirmed"
        assert comparison.deltas[0].flip == "PASS→FAIL, needs confirmation"

    def test_a_unanimous_fail_to_pass_flip_waits_for_confirmation(self):
        comparison = compare(rows("t::a", [False] * 3), rows("t::a", [True] * 3))

        assert comparison.verdict == "unconfirmed"
        assert comparison.deltas[0].flip == "FAIL→PASS, needs confirmation"

    def test_a_pass_to_fail_flip_that_holds_over_nine_votes_is_a_regression(self):
        comparison = compare(
            rows("t::a", [True] * 3),
            rows("t::a", [True, False, False]),
            confirm_before=rows("t::a", [True] * 5 + [False]),
            confirm_after=rows("t::a", [True, True] + [False] * 4),
        )

        assert comparison.verdict == "regressed"
        assert comparison.deltas[0].flip == "PASS→FAIL"

    def test_an_unstable_pass_to_fail_flip_that_holds_is_a_regression(self):
        comparison = compare(
            rows("t::a", [True, True, False]),
            rows("t::a", [True, False, False]),
            confirm_before=rows("t::a", [True] * 6),
            confirm_after=rows("t::a", [False] * 6),
        )

        assert comparison.verdict == "regressed"

    def test_a_pass_to_fail_flip_that_does_not_hold_is_not_a_regression(self):
        comparison = compare(
            rows("t::a", [True] * 3),
            rows("t::a", [True, False, False]),
            confirm_before=rows("t::a", [True] * 4 + [False] * 2),
            confirm_after=rows("t::a", [True] * 5 + [False]),
        )

        assert comparison.verdict == "neutral"
        assert comparison.deltas[0].flip == "PASS→FAIL did not hold"

    def test_a_unanimous_fail_to_pass_flip_that_holds_is_an_improvement(self):
        comparison = compare(
            rows("t::a", [False] * 3),
            rows("t::a", [True] * 3),
            confirm_before=rows("t::a", [True] + [False] * 5),
            confirm_after=rows("t::a", [True] * 6),
        )

        assert comparison.verdict == "improved"
        assert comparison.deltas[0].flip == "FAIL→PASS"

    def test_confirmation_needs_nine_votes_on_each_side(self):
        comparison = compare(
            rows("t::a", [True] * 3),
            rows("t::a", [False] * 3),
            confirm_before=rows("t::a", [True] * 6),
            confirm_after=rows("t::a", [False] * 5),
        )

        assert comparison.verdict == "unconfirmed"

    def test_a_confirmed_regression_outweighs_a_flip_still_waiting(self):
        before = rows("t::a", [True] * 3) + rows("t::b", [True] * 3)
        after = rows("t::a", [False] * 3) + rows("t::b", [False] * 3)

        comparison = compare(
            before, after, confirm_before=rows("t::a", [True] * 6), confirm_after=rows("t::a", [False] * 6)
        )

        assert comparison.verdict == "regressed"

    def test_a_confirmed_regression_outweighs_a_confirmed_gain(self):
        before = rows("t::a", [False] * 3) + rows("t::b", [True] * 3)
        after = rows("t::a", [True] * 3) + rows("t::b", [False] * 3)

        comparison = compare(
            before,
            after,
            confirm_before=rows("t::a", [False] * 6) + rows("t::b", [True] * 6),
            confirm_after=rows("t::a", [True] * 6) + rows("t::b", [False] * 6),
        )

        assert comparison.verdict == "regressed"

    def test_a_flip_waiting_for_confirmation_outweighs_an_incomplete_case(self):
        before = rows("t::a", [True] * 3) + rows("t::b", [True] * 3)
        after = rows("t::a", [False] * 3) + rows("t::b", [None] * 3)

        assert compare(before, after).verdict == "unconfirmed"

    def test_a_confirmation_run_on_another_commit_is_flagged(self):
        confirm_after = rows("t::a", [False] * 6)
        for row in confirm_after:
            row["git_sha"] = "def456"

        warnings = compare(
            rows("t::a", [True] * 3),
            rows("t::a", [False] * 3),
            confirm_before=rows("t::a", [True] * 6),
            confirm_after=confirm_after,
        ).warnings

        assert "AFTER confirmation ran on def456, not on AFTER's abc123." in warnings

    def test_confirmation_votes_leave_the_token_medians_alone(self):
        comparison = compare(
            rows("t::a", [True] * 3),
            rows("t::a", [False] * 3),
            confirm_before=rows("t::a", [True] * 6, input_tokens=90_000),
            confirm_after=rows("t::a", [False] * 6, input_tokens=90_000),
        )

        assert comparison.before_medians[SUITE]["input_tokens"] == 1000


class TestTokenMedians:
    def test_rows_without_measurements_are_left_out_of_the_median(self):
        data = rows("t::a", [True], input_tokens=1000) + rows("t::b", [True], input_tokens=None)

        assert suite_medians(data)[SUITE]["input_tokens"] == 1000


class TestLargestCaseIncrease:
    def test_a_single_case_blow_up_is_named_even_when_the_suite_median_falls(self):
        before = (
            rows("t::a", [True] * 3, input_tokens=1000)
            + rows("t::b", [True] * 3, input_tokens=2000)
            + rows("t::c", [True] * 3, input_tokens=3000)
        )
        after = (
            rows("t::a", [True] * 3, input_tokens=1000)
            + rows("t::b", [True] * 3, input_tokens=1900)
            + rows("t::c", [True] * 3, input_tokens=30000)
        )

        comparison = compare(before, after)

        assert comparison.largest_case_increase == {SUITE: ("c", 3000, 30000)}
        assert "`c` 3,000 → 30,000 (+900.0%)" in render_markdown(comparison)

    def test_a_suite_where_no_case_rose_shows_a_dash(self):
        before = rows("t::a", [True] * 3, input_tokens=1000) + rows("t::b", [True] * 3, input_tokens=2000)
        after = rows("t::a", [True] * 3, input_tokens=1000) + rows("t::b", [True] * 3, input_tokens=1500)

        comparison = compare(before, after)

        assert comparison.largest_case_increase == {SUITE: None}
        suite_row = next(line for line in render_markdown(comparison).splitlines() if line.startswith(f"| {SUITE} |"))
        assert suite_row.endswith("| – |")

    def test_cases_on_one_side_only_and_rows_without_tokens_are_ignored(self):
        before = rows("t::a", [True] * 3, input_tokens=1000) + rows("t::b", [True] * 3, input_tokens=None)
        after = (
            rows("t::a", [True] * 3, input_tokens=1000)
            + rows("t::b", [True] * 3, input_tokens=9000)
            + rows("t::c", [True] * 3, input_tokens=90000)
        )

        assert compare(before, after).largest_case_increase == {SUITE: None}


class TestOneSidedRows:
    def test_a_case_missing_on_one_side_is_listed_and_not_counted(self):
        before = rows("t::a", [True] * 3) + rows("t::b", [True] * 3)
        after = rows("t::a", [True] * 3)

        comparison = compare(before, after)

        assert comparison.missing == ["t::b"]
        assert comparison.verdict == "neutral"

    def test_a_suite_run_on_one_side_only_is_ignored(self):
        before = rows("t::a", [True] * 3) + rows("r::b", [True] * 3, suite="R")
        after = rows("t::a", [True] * 3)

        comparison = compare(before, after)

        assert comparison.missing == []
        assert comparison.one_sided_suites == ["R"]

    def test_rows_from_several_commits_in_one_file_are_flagged(self):
        before = rows("t::a", [True] * 3)
        before[0]["git_sha"] = "def456"

        assert any("2 commits" in warning for warning in compare(before, rows("t::a", [True] * 3)).warnings)

    def test_different_models_on_each_side_are_flagged(self):
        after = rows("t::a", [True] * 3)
        for row in after:
            row["model"] = "openrouter:other/model"

        assert any("BEFORE ran" in warning for warning in compare(rows("t::a", [True] * 3), after).warnings)

    def test_repeated_runs_on_one_side_are_flagged(self):
        before = rows("t::a", [True] * 3)
        before[0]["run"] = 1
        before[1]["run"] = 1
        before[2]["run"] = 2

        assert any("repeated (case, run)" in warning for warning in compare(before, rows("t::a", [True] * 3)).warnings)


class TestIncompleteCases:
    @pytest.mark.parametrize(
        "before,after",
        [
            pytest.param([True] * 3, [True, None, None], id="after"),
            pytest.param([False, None, False], [True] * 3, id="before"),
        ],
    )
    def test_a_case_that_lost_a_vote_on_either_side_is_inconclusive(self, before, after):
        comparison = compare(rows("t::a", before), rows("t::a", after))

        assert list(comparison.incomplete) == ["t::a"]
        assert comparison.verdict == "inconclusive"

    def test_a_case_with_no_vote_on_after_is_inconclusive_not_ignored(self):
        before = rows("t::a", [True] * 3) + rows("t::b", [True] * 3)
        after = rows("t::a", [True] * 3) + rows("t::b", [None] * 3)

        comparison = compare(before, after)

        assert list(comparison.incomplete) == ["t::b"]
        assert comparison.missing == []
        assert comparison.verdict == "inconclusive"

    def test_a_suite_with_no_vote_on_after_is_compared_not_ignored(self):
        before = rows("t::a", [True] * 3) + rows("r::b", [True] * 3, suite="R")
        after = rows("t::a", [True] * 3) + rows("r::b", [None] * 3, suite="R")

        comparison = compare(before, after)

        assert comparison.one_sided_suites == []
        assert comparison.verdict == "inconclusive"

    def test_a_vote_lost_without_a_row_is_inconclusive(self):
        before = rows("t::a", [True] * 3) + rows("t::b", [True] * 3)
        after = rows("t::a", [True] * 3) + rows("t::b", [True])

        assert compare(before, after).verdict == "inconclusive"

    def test_a_confirmed_regression_outweighs_an_incomplete_case(self):
        before = rows("t::a", [True] * 3) + rows("t::b", [True] * 3)
        after = rows("t::a", [False] * 3) + rows("t::b", [None] * 3)

        comparison = compare(
            before, after, confirm_before=rows("t::a", [True] * 6), confirm_after=rows("t::a", [False] * 6)
        )

        assert comparison.verdict == "regressed"

    def test_rows_without_a_vote_stay_out_of_the_token_medians(self):
        after = rows("t::a", [True, None])
        after[1]["input_tokens"] = 90_000

        assert compare(rows("t::a", [True] * 3), after).after_medians[SUITE]["input_tokens"] == 1000

    def test_rows_without_a_vote_are_not_flagged_as_unreported_usage(self):
        after = rows("t::a", [True] * 3) + rows("t::b", [None], input_tokens=0)

        assert not any("0 input tokens" in warning for warning in compare(rows("t::a", [True] * 3), after).warnings)


class TestMeasuredNothingWarnings:
    def test_two_files_with_no_case_in_common_say_nothing_was_measured(self):
        comparison = compare(rows("t::a", [True] * 3), rows("t::b", [True] * 3))

        assert "No case ran on both sides; this comparison measured nothing." in comparison.warnings

    def test_a_comparison_with_a_shared_case_does_not_say_nothing_was_measured(self):
        comparison = compare(rows("t::a", [True] * 3), rows("t::a", [True] * 3))

        assert not any("measured nothing" in warning for warning in comparison.warnings)

    def test_cases_on_one_side_only_are_counted_in_a_warning(self):
        before = rows("t::a", [True] * 3) + rows("t::b", [True] * 3)

        warnings = compare(before, rows("t::a", [True] * 3)).warnings

        assert "1 case(s) ran on one side only (see Not compared); was a case added, renamed or removed?" in warnings

    def test_no_one_sided_warning_when_every_case_ran_on_both_sides(self):
        comparison = compare(rows("t::a", [True] * 3), rows("t::a", [True] * 3))

        assert not any("one side only" in warning for warning in comparison.warnings)

    def test_a_commit_present_on_both_sides_is_flagged(self):
        comparison = compare(rows("t::a", [True] * 3), rows("t::a", [True] * 3))

        assert "BEFORE and AFTER share commit abc123; was the AFTER run on the PR branch?" in comparison.warnings

    def test_different_commits_on_each_side_are_not_flagged(self):
        after = rows("t::a", [True] * 3)
        for row in after:
            row["git_sha"] = "def456"

        assert not any("share commit" in warning for warning in compare(rows("t::a", [True] * 3), after).warnings)

    def test_rows_reporting_zero_input_tokens_are_counted_on_either_side(self):
        before = rows("t::a", [True] * 3, input_tokens=0)
        after = rows("t::a", [True] * 3)
        after[0]["input_tokens"] = 0

        warnings = compare(before, after).warnings

        assert "4 row(s) report 0 input tokens; usage may be unreported, which hides failing votes." in warnings

    def test_rows_without_a_token_count_are_not_flagged_as_zero(self):
        comparison = compare(rows("t::a", [True] * 3, input_tokens=None), rows("t::a", [True] * 3))

        assert not any("0 input tokens" in warning for warning in comparison.warnings)


class TestRecallSummary:
    def test_counts_majority_hits_clean_passes_and_noise_per_run(self):
        data = (
            rows("cr::bug1", [True, True, False], kind="bug", noise=1)
            + rows("cr::bug2", [False, False, False], kind="bug", noise=0)
            + rows("cr::clean1", [True, True, True], kind="clean", noise=0)
        )

        assert recall_summary(data) == {
            "hits": 1,
            "bug_cases": 2,
            "clean_passes": 1,
            "clean_cases": 1,
            "noise_per_run": 1.0,
            "clean_findings_per_run": 0.0,
        }

    def test_suites_without_kind_have_no_recall_summary(self):
        assert recall_summary(rows("t::a", [True])) is None

    def test_hits_count_confirmation_votes_while_noise_stays_per_initial_run(self):
        before = rows("cr::bug1", [True] * 3, kind="bug", noise=1)
        after = rows("cr::bug1", [False, False, True], kind="bug", noise=1)

        comparison = compare(
            before,
            after,
            confirm_before=rows("cr::bug1", [True] * 6, kind="bug", noise=5),
            confirm_after=rows("cr::bug1", [True] * 6, kind="bug", noise=5),
        )

        assert comparison.after_recall is not None
        assert comparison.after_recall["hits"] == 1
        assert comparison.after_recall["noise_per_run"] == 1.0


def test_markdown_carries_the_verdict_the_flip_and_the_token_table():
    comparison = compare(
        rows("t::a", [False] * 3),
        rows("t::a", [True] * 3, input_tokens=900),
        confirm_before=rows("t::a", [True] + [False] * 5),
        confirm_after=rows("t::a", [True] * 6),
    )

    markdown = render_markdown(comparison)

    assert "**Verdict: improved**" in markdown
    row = "| `a` | DAIV: Skills | FAIL 0/3 (FAIL 1/9 with re-runs) | PASS 3/3 (PASS 9/9 with re-runs) | FAIL→PASS |"
    assert row in markdown
    assert "1,000 → 900 (-10.0%)" in markdown


def test_markdown_names_the_re_runs_a_flip_needs():
    before = rows("t::a", [True] * 3) + rows("t::b", [False] * 3)
    after = rows("t::a", [False] * 3) + rows("t::b", [True] * 3)

    markdown = render_markdown(compare(before, after))

    assert "**Verdict: unconfirmed**" in markdown
    assert 'DAIV_EVAL_REPEATS=6 make eval-prompts CASES="t::a t::b" OUT=<fresh file>' in markdown


def test_median_turns_keep_their_decimal():
    before = rows("t::a", [True] * 2)
    before[1]["turns"] = 3

    markdown = render_markdown(compare(before, rows("t::a", [True] * 2, turns=3)))

    assert "2.5 → 3.0 (+20.0%)" in markdown


def test_markdown_header_names_the_commits_and_models_of_both_runs():
    before = rows("t::a", [True] * 3)
    before[0]["git_sha"] = "bbb222"
    after = rows("t::a", [True] * 3)
    for row in after:
        row["git_sha"] = "def456"
        row["model"] = "openrouter:other/model"

    lines = render_markdown(compare(before, after)).splitlines()

    assert lines[:3] == [
        "## Eval comparison",
        "",
        "BEFORE: abc123, bbb222 on openrouter:z-ai/glm-5.2 · AFTER: def456 on openrouter:other/model",
    ]


def test_main_prints_the_comparison(tmp_path, capsys):
    path = tmp_path / "run.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows("t::a", [True] * 3)) + "\n")

    assert main([str(path), str(path)]) == 0
    assert "**Verdict: neutral**" in capsys.readouterr().out


def test_markdown_lists_the_incomplete_cases_with_their_votes():
    markdown = render_markdown(compare(rows("t::a", [True] * 3), rows("t::a", [True, None, None])))

    assert "**Verdict: inconclusive**" in markdown
    assert "- `t::a`: BEFORE 3 of 3 votes, AFTER 1 of 3" in markdown


def test_main_exits_nonzero_on_an_inconclusive_comparison(tmp_path, capsys):
    before, after = tmp_path / "before.jsonl", tmp_path / "after.jsonl"
    before.write_text("".join(json.dumps(row) + "\n" for row in rows("t::a", [True] * 3)))
    after.write_text("".join(json.dumps(row) + "\n" for row in rows("t::a", [True, None, True])))

    assert main([str(before), str(after)]) == 1
    assert "**Verdict: inconclusive**" in capsys.readouterr().out


def test_main_exits_nonzero_on_a_flip_waiting_for_confirmation(tmp_path, capsys):
    before, after = tmp_path / "before.jsonl", tmp_path / "after.jsonl"
    before.write_text("".join(json.dumps(row) + "\n" for row in rows("t::a", [True] * 3)))
    after.write_text("".join(json.dumps(row) + "\n" for row in rows("t::a", [False] * 3)))

    assert main([str(before), str(after)]) == 1
    assert "**Verdict: unconfirmed**" in capsys.readouterr().out


def test_main_reads_the_confirmation_files(tmp_path, capsys):
    files = {
        "before": rows("t::a", [True] * 3),
        "after": rows("t::a", [False] * 3),
        "confirm_before": rows("t::a", [True] * 6),
        "confirm_after": rows("t::a", [False] * 6),
    }
    for name, data in files.items():
        (tmp_path / f"{name}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in data))

    exit_code = main([
        str(tmp_path / "before.jsonl"),
        str(tmp_path / "after.jsonl"),
        "--confirm",
        str(tmp_path / "confirm_before.jsonl"),
        str(tmp_path / "confirm_after.jsonl"),
    ])

    assert exit_code == 0
    assert "**Verdict: regressed**" in capsys.readouterr().out


def test_main_exits_nonzero_but_still_prints_when_no_case_ran_on_both_sides(tmp_path, capsys):
    before, after = tmp_path / "before.jsonl", tmp_path / "after.jsonl"
    before.write_text(json.dumps(rows("t::a", [True])[0]) + "\n")
    after.write_text(json.dumps(rows("t::b", [True])[0]) + "\n")

    assert main([str(before), str(after)]) == 1
    assert "this comparison measured nothing" in capsys.readouterr().out
