"""The numbers the Insights page shows: a period, compared with the period of the same length before it.

Run counts and the runs chart read ``Run.objects.visible_to`` by the local day each run was created.
A window ending today is compared with the previous one cut at the same time of day.
Session counts go through the session list's own queryset and filterset, so each count equals the
length of the list its link opens. That list filters on ``last_active_at``, which later activity moves
on, so the previous period's counts and the per-bucket sparklines count sessions with a run in the
window instead. Code velocity reads ``MergeMetric.objects.visible_to``.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from django.db.models import Avg, Count, DateField, DurationField, Exists, ExpressionWrapper, F, OuterRef, Q, Sum
from django.db.models.functions import Trunc
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.http import urlencode
from django.utils.translation import gettext_lazy as _

from sessions.filters import SCOPE_ALL, SessionFilter, local_midnight
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.templatetags.session_tags import duration

from codebase.models import MergeMetric

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.models import QuerySet

    from accounts.models import User

PERIOD_CHOICES = [
    ("today", _("Today"), 1),
    ("7d", _("7 days"), 7),
    ("30d", _("30 days"), 30),
    ("90d", _("90 days"), 90),
    ("all", _("All time"), None),
]
PERIOD_DAYS = {key: days for key, _label, days in PERIOD_CHOICES}
DEFAULT_PERIOD = "7d"

ORIGIN_SEGMENT_CSS = {
    SessionOrigin.ISSUE_WEBHOOK: "bg-amber-500/50",
    SessionOrigin.MR_WEBHOOK: "bg-cyan-500/50",
    SessionOrigin.PIPELINE_WEBHOOK: "bg-orange-500/40",
    SessionOrigin.MCP_JOB: "bg-blue-500/50",
    SessionOrigin.SCHEDULE: "bg-violet-500/40",
    SessionOrigin.API_JOB: "bg-indigo-500/50",
    SessionOrigin.UI_JOB: "bg-emerald-500/50",
    SessionOrigin.CHAT: "bg-teal-500/40",
    SessionOrigin.DELEGATED_JOB: "bg-fuchsia-500/40",
    SessionOrigin.DELEGATION_RESUME: "bg-pink-500/40",
}

RUN_OUTCOMES = [
    ("changed", _("Changed code"), "bg-status-clear", Q(status=RunStatus.SUCCESSFUL, code_changes=True)),
    ("done", _("Done, no changes"), "bg-text-muted", Q(status=RunStatus.SUCCESSFUL, code_changes=False)),
    ("input", _("Needs input"), "bg-status-found", Q(status=RunStatus.WAITING_INPUT)),
    ("failed", _("Failed"), "bg-status-fail", Q(status=RunStatus.FAILED)),
]
MERGE_KINDS = [
    ("daiv", _("Involved DAIV"), "bg-brand", Q(daiv_commits__gt=0)),
    ("people", _("People only"), "bg-text-muted", Q(daiv_commits=0)),
]

CHART_HEIGHT_PX = 120
SPARK_HEIGHT_PX = 20


@dataclasses.dataclass(frozen=True)
class Window:
    """Local calendar days from ``start`` to ``end``, both included; ``start`` is None for all time.

    ``until`` cuts the window short of the end of ``end``.
    """

    start: date | None
    end: date
    until: datetime | None = None

    @property
    def days(self) -> int | None:
        return (self.end - self.start).days + 1 if self.start is not None else None

    @property
    def upper(self) -> datetime:
        return self.until or local_midnight(self.end + timedelta(days=1))

    def previous(self) -> Window | None:
        if self.start is None:
            return None
        length = timedelta(days=self.days)
        return Window(self.start - length, self.start - timedelta(days=1), min(self.upper, timezone.now()) - length)

    @property
    def label(self) -> str:
        if self.start is None:
            return str(_("All time"))
        if self.start == self.end:
            return date_format(self.start, "j M")
        return f"{date_format(self.start, 'j M')}–{date_format(self.end, 'j M')}"

    @property
    def bucket(self) -> str:
        if self.days is None:
            return "month"
        return "day" if self.days <= 7 else "week"

    def trunc(self, field: str) -> Trunc:
        return Trunc(field, self.bucket, output_field=DateField(), tzinfo=timezone.get_current_timezone())

    def date_q(self, field: str) -> Q:
        q = Q(**{f"{field}__lt": self.upper})
        return q & Q(**{f"{field}__gte": local_midnight(self.start)}) if self.start is not None else q

    def list_params(self, *, bounded: bool = False) -> dict[str, str]:
        """Session-list params for this window; ``bounded`` adds ``date_to``, which a window ending today needs not."""
        params = {"date_from": self.start.isoformat()} if self.start is not None else {}
        if bounded:
            params["date_to"] = self.end.isoformat()
        return params


def window_for(period: str) -> Window:
    today = timezone.localdate()
    days = PERIOD_DAYS[period]
    return Window(today - timedelta(days=days - 1) if days is not None else None, today)


def session_list_url(params: dict[str, str], scope: str = SCOPE_ALL) -> str:
    return f"{reverse('session_list')}?{urlencode({'scope': scope, **params})}"


def pct(numerator: int, denominator: int) -> int | None:
    return round(numerator / denominator * 100) if denominator else None


def format_pct(percentage: int | None) -> str:
    return f"{percentage}%" if percentage is not None else "—"


def _change(current, previous, *, text=str, unit: str = "", was_unit: str | None = None) -> dict | None:
    """How ``current`` moved from ``previous``, for the line under a number; None when there is nothing to compare.

    ``was_unit`` defaults to ``unit``; a rate differs, its change is in points and its earlier value a percentage.
    """
    if current is None or previous is None:
        return None
    delta = current - previous
    direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
    was_unit = unit if was_unit is None else was_unit
    return {"direction": direction, "amount": f"{text(abs(delta))}{unit}", "was": f"{text(previous)}{was_unit}"}


def _count_change(current: int, previous: int | None) -> dict | None:
    """``_change`` plus the signed percentage a table row shows beside it."""
    if (change := _change(current, previous)) is not None:
        change["pct"] = _signed_pct(current, previous)
    return change


def _signed_pct(current: int, previous: int | None) -> str:
    if not previous or current == previous:
        return ""
    return f"{'+' if current > previous else '−'}{pct(abs(current - previous), previous)}%"


def _run_stats(runs: QuerySet[Run]) -> dict:
    duration_expr = ExpressionWrapper(F("finished_at") - F("started_at"), output_field=DurationField())
    stats = runs.aggregate(
        total=Count("id"),
        successful=Count("id", filter=Q(status=RunStatus.SUCCESSFUL)),
        failed=Count("id", filter=Q(status=RunStatus.FAILED)),
        code_changes=Count("id", filter=Q(status=RunStatus.SUCCESSFUL, code_changes=True)),
        avg_duration=Avg(duration_expr, filter=Q(status=RunStatus.SUCCESSFUL)),
    )
    stats["success_rate"] = pct(stats["successful"], stats["successful"] + stats["failed"])
    # Whole seconds, as ``duration`` shows them, so a sub-second change reads as no change.
    stats["avg_duration_s"] = int(stats["avg_duration"].total_seconds()) if stats["avg_duration"] is not None else None
    return stats


def success_rate_css(pct: int | None) -> str:
    """Text color for a success-rate percentage; the plain strong text when there is none."""
    if pct is None:
        return "text-text-strong"
    if pct >= 90:
        return "text-status-clear"
    if pct >= 75:
        return "text-status-found"
    return "text-status-fail"


def activity(user: User, window: Window, previous: Window | None) -> dict:
    """Run numbers and the runs chart over ``window``, each number compared with ``previous`` when given."""
    runs = Run.objects.visible_to(user)
    current = _run_stats(runs.filter(window.date_q("created_at")))
    before = _run_stats(runs.filter(previous.date_q("created_at"))) if previous is not None else None

    def change(key, **kwargs):
        return _change(current[key], before[key] if before else None, **kwargs)

    return {
        "total": current["total"],
        "total_change": change("total"),
        "success_rate": format_pct(current["success_rate"]),
        "success_rate_css": success_rate_css(current["success_rate"]),
        "success_rate_change": change("success_rate", unit=" pts", was_unit="%"),
        "code_changes": current["code_changes"],
        "code_changes_pct": format_pct(pct(current["code_changes"], current["successful"])),
        "code_changes_change": change("code_changes"),
        "avg_duration_s": current["avg_duration_s"],
        "avg_duration_change": change("avg_duration_s", text=duration),
        "chart": _runs_chart(runs, window),
    }


def _next_month(day: date) -> date:
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)


def _bucket_starts(window: Window, first: date | None) -> list[date]:
    start = window.start or first
    if start is None:
        return []
    if window.bucket == "day":
        return [start + timedelta(days=i) for i in range((window.end - start).days + 1)]
    if window.bucket == "week":
        monday = start - timedelta(days=start.weekday())
        return [monday + timedelta(weeks=i) for i in range((window.end - monday).days // 7 + 1)]
    starts, month = [], date(start.year, start.month, 1)
    while month <= window.end:
        starts.append(month)
        month = _next_month(month)
    return starts


def _bucket_window(bucket_start: date, window: Window) -> Window:
    if window.bucket == "day":
        end = bucket_start
    elif window.bucket == "week":
        end = bucket_start + timedelta(days=6)
    else:
        end = _next_month(bucket_start) - timedelta(days=1)
    return Window(max(bucket_start, window.start or bucket_start), min(end, window.end))


def _bucket_label(bucket_start: date, window: Window) -> str:
    if bucket_start == window.end and window.bucket == "day":
        return str(_("Today"))
    fmt = {"day": "D j", "week": "j M", "month": "M Y"}[window.bucket]
    return date_format(bucket_start, fmt)


def _grouped(rows: QuerySet, field: str, window: Window, kinds: list) -> dict[date, dict[str, int]]:
    counts = {key: Count("pk", filter=q) for key, _label, _css, q in kinds}
    grouped = (
        rows
        .filter(window.date_q(field))
        .annotate(bucket=window.trunc(field))
        .order_by()
        .values("bucket")
        .annotate(**counts)
    )
    return {row.pop("bucket"): row for row in grouped}


def stacked_chart(
    rows: QuerySet, field: str, window: Window, kinds: list, url_for: Callable[[Window], str] | None = None
) -> dict | None:
    """Columns of stacked segments, worst kind on top, sized to the tallest column; None for a one-column window."""
    if window.days == 1:
        return None
    by_bucket = _grouped(rows, field, window, kinds)
    starts = _bucket_starts(window, min(by_bucket, default=None))
    if len(starts) < 2:
        return None
    tallest = max((sum(counts.values()) for counts in by_bucket.values()), default=0) or 1
    columns = []
    for start in starts:
        counts = by_bucket.get(start, {})
        total = sum(counts.values())
        segments = [
            {"css": css, "count": counts[key], "label": label}
            for key, label, css, _q in reversed(kinds)
            if counts.get(key)
        ]
        label = _bucket_label(start, window)
        columns.append({
            "label": label,
            "total": total,
            "height_px": round(total / tallest * CHART_HEIGHT_PX),
            "segments": segments,
            "title": ", ".join([f"{label}: {total}", *(f"{s['label']} {s['count']}" for s in segments)]),
            "url": url_for(_bucket_window(start, window)) if url_for else "",
        })
    legend = [
        {"label": label, "css": css, "count": sum(c.get(key, 0) for c in by_bucket.values())}
        for key, label, css, _q in kinds
    ]
    return {
        "columns": columns,
        "legend": legend,
        "height_px": CHART_HEIGHT_PX,
        "empty": not any(column["total"] for column in columns),
    }


def _runs_chart(runs: QuerySet[Run], window: Window) -> dict | None:
    return stacked_chart(
        runs, "created_at", window, RUN_OUTCOMES, lambda bucket: session_list_url(bucket.list_params(bounded=True))
    )


def session_breakdown(user: User, window: Window, previous: Window | None) -> dict:
    """Sessions per origin over ``window`` with a per-bucket bar each, compared with ``previous`` when given.

    Counted through the list's own queryset and filterset, so each count equals the length of the list its
    link opens.
    """
    period = window.list_params()
    listed = Session.objects.listed_for(user).archived_for(user, archived=False)
    sessions = SessionFilter(period, queryset=listed).qs
    rows = list(
        sessions
        .order_by()
        .values("origin")
        .annotate(n=Count("pk"), failed=Count("pk", filter=Q(latest_run_status=RunStatus.FAILED)))
    )
    origin_counts = {row["origin"]: row["n"] for row in rows}
    total = sum(origin_counts.values())

    previous_counts = None
    if previous is not None:
        ran = Run.objects.filter(previous.date_q("created_at"), session=OuterRef("pk"))
        previous_counts = dict(
            listed.filter(Exists(ran)).order_by().values("origin").annotate(n=Count("pk")).values_list("origin", "n")
        )

    origin_sparks = _origin_sparks(sessions, window)
    segments = []
    for origin in SessionOrigin.values:
        n = origin_counts.get(origin)
        if not n:
            continue
        segments.append({
            "label": SessionOrigin(origin).label,
            "value": n,
            "change": _count_change(n, previous_counts.get(origin, 0) if previous_counts is not None else None),
            "pct": round(n / total * 100, 1),
            "css": ORIGIN_SEGMENT_CSS[origin],
            "url": session_list_url({**period, "trigger": origin}),
            "spark": origin_sparks.get(origin, []),
        })
    return {
        "sessions_total": total,
        "sessions_change": _count_change(total, sum(previous_counts.values()) if previous_counts is not None else None),
        "sessions_url": session_list_url(period),
        "segments": segments,
        "sessions_failed": sum(row["failed"] for row in rows),
        "sessions_failed_url": session_list_url({**period, "status": RunStatus.FAILED}),
        "has_sparks": any(segment["spark"] for segment in segments),
    }


def _origin_sparks(sessions: QuerySet[Session], window: Window) -> dict[str, list[dict]]:
    """Per origin, how many of ``sessions`` had a run in each bucket."""
    if window.days == 1:
        return {}
    per_bucket_rows = (
        Run.objects
        .filter(window.date_q("created_at"), session__in=sessions.values("pk"))
        .annotate(bucket=window.trunc("created_at"))
        .order_by()
        .values("session__origin", "bucket")
        .annotate(n=Count("session", distinct=True))
    )
    counts: dict[str, dict[date, int]] = {}
    for row in per_bucket_rows:
        counts.setdefault(row["session__origin"], {})[row["bucket"]] = row["n"]
    starts = _bucket_starts(window, min((bucket for per in counts.values() for bucket in per), default=None))
    if len(starts) < 2:
        return {}
    sparks = {}
    for origin, per_bucket in counts.items():
        tallest = max(per_bucket.values())
        sparks[origin] = [
            {"count": n, "height_px": max(round(n / tallest * SPARK_HEIGHT_PX), 2)}
            for n in (per_bucket.get(start, 0) for start in starts)
        ]
    return sparks


def velocity(user: User, window: Window) -> dict | None:
    """Merges into default branches of the repositories ``user`` can read; None when there were none."""
    merges = MergeMetric.objects.visible_to(user)
    stats = merges.filter(window.date_q("merged_at")).aggregate(
        total=Count("id"),
        lines_added=Sum("lines_added", default=0),
        lines_removed=Sum("lines_removed", default=0),
        daiv_merges=Count("id", filter=Q(daiv_commits__gt=0)),
        total_commits=Sum("total_commits", default=0),
        daiv_commits=Sum("daiv_commits", default=0),
    )
    if not stats["total"]:
        return None
    return {
        "total_merges": stats["total"],
        "daiv_merges": stats["daiv_merges"],
        "daiv_merges_pct": format_pct(pct(stats["daiv_merges"], stats["total"])),
        "daiv_commits": stats["daiv_commits"],
        "total_commits": stats["total_commits"],
        "daiv_commits_pct": format_pct(pct(stats["daiv_commits"], stats["total_commits"])),
        "lines_added": stats["lines_added"],
        "lines_removed": stats["lines_removed"],
        "chart": stacked_chart(merges, "merged_at", window, MERGE_KINDS),
    }
