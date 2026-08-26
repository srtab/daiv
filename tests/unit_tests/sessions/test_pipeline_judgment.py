import pytest
from sessions.pipeline_watch import Judgment, failed_jobs, judge_pipeline

from .conftest import make_job, make_pipeline


def test_success_with_jobs_is_green():
    pipeline = make_pipeline("success", [make_job("success")])
    assert judge_pipeline(pipeline) is Judgment.GREEN


def test_a_real_failure_is_actionable():
    pipeline = make_pipeline("failed", [make_job("success"), make_job("failed", name="lint")])
    assert judge_pipeline(pipeline) is Judgment.ACTIONABLE


def test_an_allow_failure_job_is_not_a_failure():
    pipeline = make_pipeline("failed", [make_job("failed", allow_failure=True, name="flaky")])
    assert judge_pipeline(pipeline) is Judgment.GREEN


def test_a_real_failure_beside_an_allowed_one_is_still_actionable():
    pipeline = make_pipeline(
        "failed", [make_job("failed", allow_failure=True, name="flaky"), make_job("failed", name="tests")]
    )
    assert judge_pipeline(pipeline) is Judgment.ACTIONABLE


def test_a_pipeline_with_no_jobs_is_unclear():
    # The GitLab private cross-project include case: CI resolved as an identity that
    # could not read the include, so nothing ran. Not green, not fixable.
    assert judge_pipeline(make_pipeline("success", [])) is Judgment.UNCLEAR


@pytest.mark.parametrize("status", ["blocked", "manual", "skipped", "canceled"])
def test_statuses_needing_a_human_are_unclear(status):
    pipeline = make_pipeline(status, [make_job("manual")])
    assert judge_pipeline(pipeline) is Judgment.UNCLEAR


@pytest.mark.parametrize("status", ["running", "pending", "created"])
def test_a_pipeline_still_going_is_unclear(status):
    pipeline = make_pipeline(status, [make_job("running")])
    assert judge_pipeline(pipeline) is Judgment.UNCLEAR


def test_a_missing_pipeline_is_unclear():
    assert judge_pipeline(None) is Judgment.UNCLEAR


def test_failed_is_actionable_even_with_no_failed_job_visible():
    # A pipeline can fail for a reason no job reports (e.g. a config error). Still worth a look.
    assert judge_pipeline(make_pipeline("failed", [make_job("success")])) is Judgment.ACTIONABLE


def test_failed_jobs_excludes_allowed_failures():
    pipeline = make_pipeline(
        "failed", [make_job("failed", allow_failure=True, name="flaky"), make_job("failed", name="tests")]
    )
    assert [job.name for job in failed_jobs(pipeline)] == ["tests"]
