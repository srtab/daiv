import logging
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codebase.base import Job, Pipeline

logger = logging.getLogger("daiv.sessions")

TERMINAL_PIPELINE_STATUSES = frozenset({"success", "failed", "canceled", "skipped"})
NEEDS_A_HUMAN_STATUSES = frozenset({"blocked", "manual", "canceled", "skipped"})


class Judgment(StrEnum):
    GREEN = "green"
    ACTIONABLE = "actionable"
    UNCLEAR = "unclear"


def failed_jobs(pipeline: Pipeline) -> list[Job]:
    """Jobs whose failure the project has not declared acceptable."""
    return [job for job in pipeline.jobs if job.is_failed() and not job.allow_failure]


def judge_pipeline(pipeline: Pipeline | None) -> Judgment:
    """Decide whether a pipeline is green, worth an agent run, or not ours to judge.

    Deliberately conservative: anything that is not an unambiguous pass or an unambiguous
    failure is ``UNCLEAR``, which stops the watch instead of spending an attempt.
    """
    if pipeline is None:
        return Judgment.UNCLEAR
    if pipeline.status in NEEDS_A_HUMAN_STATUSES:
        return Judgment.UNCLEAR
    if pipeline.status not in TERMINAL_PIPELINE_STATUSES:
        return Judgment.UNCLEAR
    if not pipeline.jobs:
        return Judgment.UNCLEAR
    if pipeline.status == "failed":
        real_failures = failed_jobs(pipeline)
        # A failed pipeline whose only failed jobs are all allow_failure is effectively green.
        # A pipeline with no failed jobs visible (e.g. config error) is still worth a look.
        if real_failures or not any(job.is_failed() for job in pipeline.jobs):
            return Judgment.ACTIONABLE
        return Judgment.GREEN
    return Judgment.GREEN
