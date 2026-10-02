"""Alias for one release: rows queued before ``run_job_task`` moved still store ``jobs.tasks.run_job_task``."""

from sessions.executor.tasks import run_job_task

__all__ = ["run_job_task"]
