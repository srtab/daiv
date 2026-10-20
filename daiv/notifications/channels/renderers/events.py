"""What each event says, written once for every chat channel.

Each definition turns ``Notification.context`` into ordered :class:`Row` s; a channel's formatter
decides how a row looks. ``EVENT_ROWS`` is the one event → definition registry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from notifications.channels.renderers.base import Row, breakdown_value, fmt_duration, repo_list, usage_rows
from notifications.choices import EventType

if TYPE_CHECKING:
    from collections.abc import Callable

    from notifications.models import Notification


def _job_finished(ctx: dict) -> list[Row]:
    return [
        Row("Trigger", ctx.get("trigger_label") or "—"),
        Row("Duration", fmt_duration(ctx.get("duration_seconds"))),
        *usage_rows(ctx),
    ]


def _schedule_finished(ctx: dict) -> list[Row]:
    return [
        Row("Repository", ctx.get("repo_id") or "—"),
        Row("Owner", ctx.get("trigger_owner") or "—"),
        Row("Duration", fmt_duration(ctx.get("duration_seconds"))),
        *usage_rows(ctx),
    ]


def _job_batch_finished(ctx: dict) -> list[Row]:
    rows = [
        Row("Results", f"⚑ {ctx.get('notable_count', 0)} · ✓ {ctx.get('all_clear_count', 0)} of {ctx.get('total', 0)}"),
        Row("Breakdown", breakdown_value(ctx)),
        Row("Duration", fmt_duration(ctx.get("duration_seconds"))),
    ]
    if owner := ctx.get("trigger_owner"):
        rows.append(Row("Owner", owner))
    rows += usage_rows(ctx, cost_label="Total cost")
    if repo_ids := ctx.get("repo_ids"):
        rows.append(Row("Repositories", repo_list(repo_ids), wide=True))
    return rows


def _pipeline_watch_exhausted(ctx: dict) -> list[Row]:
    rows = [Row("Repository", ctx.get("repo_id") or "—")]
    if iid := ctx.get("merge_request_iid"):
        rows.append(Row("Merge request", f"!{iid}"))
    rows.append(Row("Attempts", str(ctx.get("attempts") or "—")))
    if jobs := ctx.get("failing_jobs"):
        rows.append(Row("Failing jobs", ", ".join(jobs), wide=True))
    if pipeline_url := ctx.get("pipeline_url"):
        # Already absolute (a CI host URL), so it must not go through link_of().
        rows.append(Row("Pipeline", pipeline_url, wide=True))
    return rows


def _schedule_dispatch_failed(ctx: dict) -> list[Row]:
    repo_ids = ctx.get("repo_ids") or []
    return [
        Row("Repositories" if len(repo_ids) > 1 else "Repository", repo_list(repo_ids) or "—"),
        Row("Last run", ctx.get("last_run") or "—"),
        Row("Reason", ctx.get("reason") or "—", wide=True),
    ]


EVENT_ROWS: dict[str, Callable[[dict], list[Row]]] = {
    EventType.JOB_FINISHED: _job_finished,
    EventType.SCHEDULE_FINISHED: _schedule_finished,
    EventType.JOB_BATCH_FINISHED: _job_batch_finished,
    EventType.PIPELINE_WATCH_EXHAUSTED: _pipeline_watch_exhausted,
    EventType.SCHEDULE_DISPATCH_FAILED: _schedule_dispatch_failed,
}


def event_rows(notification: Notification) -> list[Row] | None:
    """The notification's rows, or ``None`` for an event with no definition yet (channels send plain text)."""
    build = EVENT_ROWS.get(notification.event_type)
    return None if build is None else build(notification.context)
