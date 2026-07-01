from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db.models import Q

from asgiref.sync import sync_to_async

from schedules.models import Frequency, ScheduledJob

if TYPE_CHECKING:
    from datetime import datetime


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


def compute_next_run_or_raise(instance: ScheduledJob) -> None:
    """Set ``next_run_at`` for an enabled schedule, mapping ``compute_next_run()``'s
    ``ValueError`` to ``ValidationError``.

    Shared by ``ScheduledJobCreateForm.save()`` and ``acreate_scheduled_job`` so the
    "compute next run when enabled" rule lives in one place. ``compute_next_run`` only
    raises ``ValueError`` for a config that ``_validate_frequency_fields`` already rejects,
    so this is reachable only via a microsecond-wide TOCTOU on the ONCE 60s boundary;
    mapping it keeps both callers' "raises only ValidationError" contract total.
    """
    if not instance.is_enabled:
        return
    try:
        instance.compute_next_run()
    except ValueError as err:
        raise ValidationError(str(err)) from err


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
    compute_next_run_or_raise(instance)
    await instance.asave()
    return instance


async def alist_scheduled_jobs(
    user,
    *,
    enabled_only: bool = False,
    repo_id: str | None = None,
    limit: int | None = None,
    before: tuple[datetime, int] | None = None,
) -> list[ScheduledJob]:
    """Return ``user``'s schedules, newest first.

    ``before`` is a keyset cursor ``(created, id)`` of the last row already seen; only rows
    strictly older (in ``-created, -id`` order) are returned. ``limit=None`` returns all rows.

    ``repo_id`` filters to schedules whose ``repos`` JSON list contains that id. That match
    runs in Python (not a ``repos__contains`` query) because the test DB is SQLite, which
    doesn't support JSON containment lookups. This splits the query into two paths:

    * **no ``repo_id``** — the common case — bounds the query with a SQL ``LIMIT`` (``qs[:limit]``),
      like the sibling list services.
    * **``repo_id`` set** — cannot push the filter into SQL, so it fetches the ordered rows
      (Django materialises the whole result set on ``async for``; per-user schedule counts are
      small, so this is cheap) and keeps them until ``limit`` *matching* rows are collected —
      never short-changing a page with non-matching rows that sort ahead of matches.
    """
    qs = ScheduledJob.objects.filter(user=user)
    if enabled_only:
        qs = qs.filter(is_enabled=True)
    if before is not None:
        created, last_id = before
        qs = qs.filter(Q(created__lt=created) | Q(created=created, id__lt=last_id))
    qs = qs.order_by("-created", "-id")

    if repo_id is None:
        if limit is not None:
            qs = qs[:limit]
        return [job async for job in qs]

    rows: list[ScheduledJob] = []
    async for job in qs:
        if any(r.get("repo_id") == repo_id for r in (job.repos or [])):
            rows.append(job)
            if limit is not None and len(rows) >= limit:
                break
    return rows
