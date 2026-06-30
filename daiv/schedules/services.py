from asgiref.sync import sync_to_async

from schedules.models import Frequency, ScheduledJob


def clear_irrelevant_frequency_fields(cleaned_data: dict) -> dict:
    """Drop stale ``cron_expression`` / ``time`` / ``run_at`` for the chosen frequency so
    leftover values from another frequency don't trip ``_validate_frequency_fields``."""
    frequency = cleaned_data.get("frequency")
    if frequency != Frequency.CUSTOM:
        cleaned_data["cron_expression"] = ""
    if frequency in (Frequency.HOURLY, Frequency.CUSTOM, Frequency.ONCE):
        cleaned_data["time"] = None
    if frequency != Frequency.ONCE:
        cleaned_data["run_at"] = None
    return cleaned_data


async def acreate_scheduled_job(user, **fields) -> ScheduledJob:
    """Build, validate, and persist a ScheduledJob for ``user``.

    Mirrors ScheduledJobCreateForm.save(): clear irrelevant frequency fields, then
    ``full_clean()`` (runs the model's repos coercion + ``_validate_frequency_fields``),
    then ``compute_next_run()`` for enabled jobs, then save.

    ``full_clean()`` issues DB queries (FK + constraint checks), so it runs via
    ``sync_to_async``. Raises ``django.core.exceptions.ValidationError`` on invalid input.
    """
    fields = clear_irrelevant_frequency_fields(fields)
    instance = ScheduledJob(user=user, **fields)
    await sync_to_async(instance.full_clean)()
    if instance.is_enabled:
        instance.compute_next_run()
    await instance.asave()
    return instance


async def alist_scheduled_jobs(user, *, enabled_only: bool = False, repo_id: str | None = None) -> list[ScheduledJob]:
    """Return ``user``'s schedules, newest first. ``repo_id`` filters (in Python) to
    schedules whose ``repos`` JSON list contains that id."""
    qs = ScheduledJob.objects.filter(user=user)
    if enabled_only:
        qs = qs.filter(is_enabled=True)
    rows = [job async for job in qs.order_by("-created")]
    if repo_id:
        rows = [job for job in rows if any(r.get("repo_id") == repo_id for r in (job.repos or []))]
    return rows
