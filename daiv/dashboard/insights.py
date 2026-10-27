"""The numbers the Insights page shows: a period, compared with the period of the same length before it.

Run counts and the runs chart read ``Run.objects.visible_to`` by the local day each run was created.
Session counts go through the session list's own queryset and filterset, so each count equals the
length of the list its link opens. Code velocity reads the ``MergeMetric`` rows of the repositories the
viewer can read; an admin reads them all.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from typing import TYPE_CHECKING

from django.db.models import Avg, Count, DateField, DurationField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Trunc
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.http import urlencode
from django.utils.translation import gettext_lazy as _

from sessions.filters import SCOPE_ALL, SessionFilter
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.templatetags.session_tags import duration

from codebase.authorization import viewable_repo_ids_subquery
from codebase.models import MergeMetric

if TYPE_CHECKING:
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

# Hues follow sessions/_origin_badge.html, so a segment matches the badges on the list it opens.
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
    """Local calendar days from ``start`` to ``end``, both included; ``start`` is None for all time."""

    start: date | None
    end: date

    @property
    def days(self) -> int | None:
        return (self.end - self.start).days + 1 if self.start is not None else None

    def previous(self) -> Window | None:
        if self.start is None:
            return None
        length = self.end - self.start + timedelta(days=1)
        return Window(self.start - length, self.start - timedelta(days=1))

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

    def date_q(self, field: str) -> Q:
        q = Q(**{f"{field}__date__lte": self.end})
        return q & Q(**{f"{field}__date__gte": self.start}) if self.start is not None else q

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


def session_list_url(params: dict[str, str]) -> str:
    return f"{reverse('session_list')}?{urlencode({'scope': SCOPE_ALL, **params})}"


def _pct(numerator: int, denominator: int) -> int | None:
    return round(numerator / denominator * 100) if denominator else None


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


def _visible_runs(user: User) -> QuerySet[Run]:
    # ``visible_to`` is DISTINCT over a join; grouping it directly would count a run once per joined row.
    return Run.objects.filter(pk__in=Run.objects.visible_to(user).values("pk"))


def _run_stats(runs: QuerySet[Run]) -> dict:
    duration_expr = ExpressionWrapper(F("finished_at") - F("started_at"), output_field=DurationField())
    stats = runs.aggregate(
        total=Count("id"),
        successful=Count("id", filter=Q(status=RunStatus.SUCCESSFUL)),
        failed=Count("id", filter=Q(status=RunStatus.FAILED)),
        code_changes=Count("id", filter=Q(status=RunStatus.SUCCESSFUL, code_changes=True)),
        avg_duration=Avg(duration_expr, filter=Q(status=RunStatus.SUCCESSFUL)),
    )
    stats["success_rate"] = _pct(stats["successful"], stats["successful"] + stats["failed"])
    stats["avg_duration_s"] = stats["avg_duration"].total_seconds() if stats["avg_duration"] is not None else None
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


def activity(user: User, window: Window) -> dict:
    runs = _visible_runs(user)
    current = _run_stats(runs.filter(window.date_q("created_at")))
    previous_window = window.previous()
    previous = _run_stats(runs.filter(previous_window.date_q("created_at"))) if previous_window else None

    def change(key, **kwargs):
        return _change(current[key], previous[key] if previous else None, **kwargs)

    return {
        "total": current["total"],
        "total_change": change("total"),
        "success_rate": f"{current['success_rate']}%" if current["success_rate"] is not None else "—",
        "success_rate_css": success_rate_css(current["success_rate"]),
        "success_rate_change": change("success_rate", unit=" pts", was_unit="%"),
        "failed": current["failed"],
        "code_changes": current["code_changes"],
        "code_changes_pct": f"{p}%" if (p := _pct(current["code_changes"], current["successful"])) is not None else "—",
        "code_changes_change": change("code_changes"),
        "avg_duration_s": current["avg_duration_s"],
        "avg_duration_change": change("avg_duration_s", text=duration),
        "chart": _runs_chart(runs, window),
    }


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
        month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
    return starts


def _bucket_window(bucket_start: date, window: Window) -> Window:
    if window.bucket == "day":
        end = bucket_start
    elif window.bucket == "week":
        end = bucket_start + timedelta(days=6)
    else:
        end = date(bucket_start.year + bucket_start.month // 12, bucket_start.month % 12 + 1, 1) - timedelta(days=1)
    return Window(max(bucket_start, window.start or bucket_start), min(end, window.end))


def _bucket_label(bucket_start: date, window: Window) -> str:
    if bucket_start == window.end and window.bucket == "day":
        return str(_("Today"))
    fmt = {"day": "D j", "week": "j M", "month": "M Y"}[window.bucket]
    return date_format(bucket_start, fmt)


def _grouped(rows: QuerySet, field: str, window: Window, kinds: list) -> dict[date, dict[str, int]]:
    bucket = Trunc(field, window.bucket, output_field=DateField(), tzinfo=timezone.get_current_timezone())
    counts = {key: Count("pk", filter=q) for key, _label, _css, q in kinds}
    grouped = rows.filter(window.date_q(field)).annotate(bucket=bucket).order_by().values("bucket").annotate(**counts)
    return {row.pop("bucket"): row for row in grouped}


def _stacked_chart(by_bucket: dict[date, dict[str, int]], window: Window, kinds: list, url_for) -> dict | None:
    """Columns of stacked segments, worst kind on top, sized to the tallest column; None for a one-column window."""
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
            "url": url_for(_bucket_window(start, window)),
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
    by_bucket = _grouped(runs, "created_at", window, RUN_OUTCOMES)
    return _stacked_chart(
        by_bucket, window, RUN_OUTCOMES, lambda bucket: session_list_url(bucket.list_params(bounded=True))
    )


def session_breakdown(user: User, window: Window) -> dict:
    """Sessions per origin over ``window``, with the previous window's count and a per-bucket sparkline.

    Counted through the list's own queryset and filterset, so each count equals the length of the list its
    link opens.
    """
    period = window.list_params()
    listed = Session.objects.listed_for(user)
    sessions = SessionFilter(period, queryset=listed).qs
    rows = list(
        sessions
        .order_by()
        .values("origin")
        .annotate(n=Count("pk"), failed=Count("pk", filter=Q(latest_run_status=RunStatus.FAILED)))
    )
    origin_counts = {row["origin"]: row["n"] for row in rows}
    total = sum(origin_counts.values())

    previous_window = window.previous()
    previous_counts = {}
    if previous_window is not None:
        previous_sessions = SessionFilter(previous_window.list_params(bounded=True), queryset=listed).qs
        previous_counts = dict(
            previous_sessions.order_by().values("origin").annotate(n=Count("pk")).values_list("origin", "n")
        )

    sparks = _origin_sparks(sessions, window)
    segments = []
    for origin in SessionOrigin.values:
        n = origin_counts.get(origin)
        if not n:
            continue
        previous = previous_counts.get(origin, 0) if previous_window is not None else None
        segments.append({
            "label": SessionOrigin(origin).label,
            "value": n,
            "previous": previous,
            "change": _change(n, previous),
            "change_pct": _signed_pct(n, previous),
            "pct": round(n / total * 100, 1),
            "css": ORIGIN_SEGMENT_CSS[origin],
            "url": session_list_url({**period, "trigger": origin}),
            "spark": sparks.get(origin, []),
        })
    previous_total = sum(previous_counts.values()) if previous_window is not None else None
    return {
        "sessions_total": total,
        "sessions_previous": previous_total,
        "sessions_change": _change(total, previous_total),
        "sessions_change_pct": _signed_pct(total, previous_total),
        "sessions_url": session_list_url(period),
        "segments": segments,
        "sessions_failed": sum(row["failed"] for row in rows),
        "sessions_failed_url": session_list_url({**period, "status": RunStatus.FAILED}),
        "previous_window": previous_window,
        "has_sparks": any(segment["spark"] for segment in segments),
    }


def _signed_pct(current: int, previous: int | None) -> str:
    if not previous or current == previous:
        return ""
    return f"{'+' if current > previous else '−'}{_pct(abs(current - previous), previous)}%"


def _origin_sparks(sessions: QuerySet[Session], window: Window) -> dict[str, list[dict]]:
    starts = _bucket_starts(window, None if window.start else _first_day(sessions))
    if len(starts) < 2:
        return {}
    bucket = Trunc("last_active_at", window.bucket, output_field=DateField(), tzinfo=timezone.get_current_timezone())
    counts: dict[str, dict[date, int]] = {}
    for row in sessions.annotate(bucket=bucket).order_by().values("origin", "bucket").annotate(n=Count("pk")):
        counts.setdefault(row["origin"], {})[row["bucket"]] = row["n"]
    sparks = {}
    for origin, per_bucket in counts.items():
        tallest = max(per_bucket.values())
        sparks[origin] = [
            {"count": n, "height_px": max(round(n / tallest * SPARK_HEIGHT_PX), 2)}
            for n in (per_bucket.get(start, 0) for start in starts)
        ]
    return sparks


def _first_day(sessions: QuerySet[Session]) -> date | None:
    first = sessions.order_by("last_active_at").values_list("last_active_at", flat=True).first()
    return timezone.localdate(first) if first is not None else None


def merges_for(user: User) -> QuerySet[MergeMetric]:
    if user.is_admin:
        return MergeMetric.objects.all()
    return MergeMetric.objects.filter(repo_id__in=viewable_repo_ids_subquery(user))


def velocity(user: User, window: Window) -> dict | None:
    """Merges into default branches of the repositories ``user`` can read; None when there were none."""
    merges = merges_for(user)
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
    by_bucket = _grouped(merges, "merged_at", window, MERGE_KINDS)
    return {
        "total_merges": stats["total"],
        "daiv_merges": stats["daiv_merges"],
        "daiv_merges_pct": _pct(stats["daiv_merges"], stats["total"]),
        "daiv_commits": stats["daiv_commits"],
        "total_commits": stats["total_commits"],
        "daiv_commits_pct": _pct(stats["daiv_commits"], stats["total_commits"]),
        "lines_added": stats["lines_added"],
        "lines_removed": stats["lines_removed"],
        "chart": _stacked_chart(by_bucket, window, MERGE_KINDS, lambda bucket: ""),
    }
