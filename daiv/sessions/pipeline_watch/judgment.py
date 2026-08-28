"""Whether a pipeline is green, worth an agent run, or not ours to judge. No I/O."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codebase.base import Job, Pipeline

# The only two statuses that are a verdict. Everything else either waits on a human or has
# not got there yet (created, pending, running, preparing, …), and both read as UNCLEAR.
VERDICT_STATUSES = frozenset({"success", "failed"})
NEEDS_A_HUMAN_STATUSES = frozenset({"blocked", "manual", "canceled", "skipped"})
JUDGEABLE_PIPELINE_STATUSES = VERDICT_STATUSES | NEEDS_A_HUMAN_STATUSES


class Judgment(StrEnum):
    GREEN = "green"
    ACTIONABLE = "actionable"
    UNCLEAR = "unclear"


class PipelineReport:
    """A pipeline and the verdict it implies, computed once.

    Deliberately conservative: anything that is not an unambiguous pass or an unambiguous
    failure is ``UNCLEAR``, which stops the watch instead of spending an attempt.
    """

    def __init__(self, pipeline: Pipeline) -> None:
        self.pipeline = pipeline
        self.failed_jobs: list[Job] = [job for job in pipeline.jobs if job.is_failed() and not job.allow_failure]
        self.judgment = self._judge()

    @classmethod
    def of(cls, pipeline: Pipeline | None) -> PipelineReport | None:
        """``None`` for a pipeline that could not be read, which the caller reads as unclear."""
        return None if pipeline is None else cls(pipeline)

    def _judge(self) -> Judgment:
        if self.pipeline.status not in VERDICT_STATUSES or not self.pipeline.jobs:
            return Judgment.UNCLEAR
        if self.pipeline.status == "success":
            return Judgment.GREEN
        # A failure with no failed job visible at all (e.g. a config error) is still worth a look.
        failing = [job for job in self.pipeline.jobs if job.is_failed()]
        if failing and all(job.allow_failure for job in failing):
            return Judgment.GREEN
        return Judgment.ACTIONABLE

    @property
    def is_judgeable(self) -> bool:
        """Whether the pipeline has settled into a status worth judging at all."""
        return self.pipeline.status in JUDGEABLE_PIPELINE_STATUSES

    def failed_job_names(self, default: str = "") -> str:
        """Comma-separated names of the jobs whose failure counts, or ``default`` when none are visible."""
        return ", ".join(job.name for job in self.failed_jobs) or default

    @property
    def id(self) -> int:
        return self.pipeline.id

    @property
    def sha(self) -> str:
        return self.pipeline.sha

    @property
    def status(self) -> str:
        return self.pipeline.status

    @property
    def web_url(self) -> str:
        return self.pipeline.web_url
