import pytest
from sessions.pipeline_watch.judgment import JUDGEABLE_PIPELINE_STATUSES, Judgment, PipelineReport

from ..conftest import make_job, make_pipeline


def test_success_with_jobs_is_green():
    pipeline = make_pipeline("success", [make_job("success")])
    assert PipelineReport(pipeline).judgment is Judgment.GREEN


def test_a_real_failure_is_actionable():
    pipeline = make_pipeline("failed", [make_job("success"), make_job("failed", name="lint")])
    assert PipelineReport(pipeline).judgment is Judgment.ACTIONABLE


def test_an_allow_failure_job_is_not_a_failure():
    pipeline = make_pipeline("failed", [make_job("failed", allow_failure=True, name="flaky")])
    assert PipelineReport(pipeline).judgment is Judgment.GREEN


def test_a_real_failure_beside_an_allowed_one_is_still_actionable():
    pipeline = make_pipeline(
        "failed", [make_job("failed", allow_failure=True, name="flaky"), make_job("failed", name="tests")]
    )
    assert PipelineReport(pipeline).judgment is Judgment.ACTIONABLE


def test_a_pipeline_with_no_jobs_is_unclear():
    # The GitLab private cross-project include case: CI resolved as an identity that
    # could not read the include, so nothing ran. Not green, not fixable.
    assert PipelineReport(make_pipeline("success", [])).judgment is Judgment.UNCLEAR


@pytest.mark.parametrize("status", ["blocked", "manual", "skipped", "canceled"])
def test_statuses_needing_a_human_are_unclear(status):
    pipeline = make_pipeline(status, [make_job("manual")])
    assert PipelineReport(pipeline).judgment is Judgment.UNCLEAR


@pytest.mark.parametrize("status", ["running", "pending", "created"])
def test_a_pipeline_still_going_is_unclear(status):
    pipeline = make_pipeline(status, [make_job("running")])
    assert PipelineReport(pipeline).judgment is Judgment.UNCLEAR


def test_a_missing_pipeline_has_no_report():
    """The caller reads ``None`` as unclear; this is the one signature the refactor changed."""
    assert PipelineReport.of(None) is None


def test_failed_is_actionable_even_with_no_failed_job_visible():
    # A pipeline can fail for a reason no job reports (e.g. a config error). Still worth a look.
    assert PipelineReport(make_pipeline("failed", [make_job("success")])).judgment is Judgment.ACTIONABLE


def test_failed_jobs_excludes_allowed_failures():
    pipeline = make_pipeline(
        "failed", [make_job("failed", allow_failure=True, name="flaky"), make_job("failed", name="tests")]
    )
    assert [job.name for job in PipelineReport(pipeline).failed_jobs] == ["tests"]


def test_failed_job_names_falls_back_to_its_default():
    report = PipelineReport(make_pipeline("failed", [make_job("failed", allow_failure=True, name="flaky")]))
    assert report.failed_job_names(default="the pipeline") == "the pipeline"


def test_the_failed_jobs_are_computed_once():
    """Three call sites read them per evaluation; the report is what stops that being three passes."""
    report = PipelineReport(make_pipeline("failed", [make_job("failed", name="tests")]))
    assert report.failed_jobs is report.failed_jobs


@pytest.mark.parametrize("status", sorted(JUDGEABLE_PIPELINE_STATUSES))
def test_a_judgeable_status_is_marked_judgeable(status):
    assert PipelineReport(make_pipeline(status, [make_job("failed")])).is_judgeable is True


@pytest.mark.parametrize("status", ["running", "pending", "created", "preparing"])
def test_a_status_still_in_flight_is_not_judgeable(status):
    assert PipelineReport(make_pipeline(status, [make_job("running")])).is_judgeable is False
