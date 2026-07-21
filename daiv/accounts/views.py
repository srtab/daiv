import logging
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db import IntegrityError
from django.db.models import Avg, Count, DurationField, Exists, ExpressionWrapper, F, OuterRef, Q, Subquery, Sum
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.timezone import localdate
from django.views import View
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, ListView, TemplateView, UpdateView

from django_filters.views import FilterView
from notifications.choices import EventType
from notifications.models import Notification
from sessions.models import EnvelopeStatus, Run, RunEnvelope, RunStatus, SessionOrigin
from sessions.reconcile import still_actionable

from accounts.context_processors import running_jobs_count
from accounts.emails import send_welcome_email
from accounts.filters import UserFilter
from accounts.forms import APIKeyCreateForm, UserCreateForm, UserUpdateForm
from accounts.mixins import AdminRequiredMixin, BreadcrumbMixin
from accounts.models import APIKey, User
from codebase.models import MergeMetric
from core.utils import is_htmx
from schedules.models import ScheduledJob

logger = logging.getLogger(__name__)


def homepage(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "accounts/homepage.html")


PICKER_USERS_LIMIT = 10
PICKER_USERS_MIN_QUERY = 2


class UserPickerView(LoginRequiredMixin, TemplateView):
    """HTMX fragment: up to ``PICKER_USERS_LIMIT`` active users matching ``?q=``.

    Excludes the requesting user and any ids passed in ``?exclude=`` (CSV).
    """

    template_name = "accounts/_user_picker_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query = self.request.GET.get("q", "").strip()
        if len(query) < PICKER_USERS_MIN_QUERY:
            context["users"] = []
            return context

        exclude_ids: set[int] = {self.request.user.pk}
        for part in self.request.GET.get("exclude", "").split(","):
            stripped = part.strip()
            if stripped.isdigit():
                exclude_ids.add(int(stripped))

        context["users"] = (
            User.objects
            .filter(is_active=True)
            .filter(Q(username__icontains=query) | Q(email__icontains=query) | Q(name__icontains=query))
            .exclude(pk__in=exclude_ids)
            .only("pk", "username", "name", "email")
            .order_by("username")[:PICKER_USERS_LIMIT]
        )
        return context


PERIOD_CHOICES = [
    ("today", "Today", 0),
    ("7d", "7 days", 7),
    ("30d", "30 days", 30),
    ("90d", "90 days", 90),
    ("all", "All time", None),
]
PERIOD_DAYS = {key: days for key, _, days in PERIOD_CHOICES}
# The personal console lands on the week (Story 3.1 / D4): a weekly throughput number is
# screenshot-worthy where a daily one is not, and it satisfies the FR-16 "this week" headline.
# ``ManagerLensView`` is unaffected — it passes ``default="all"`` explicitly.
DEFAULT_PERIOD = "7d"


def _raw_pct(numerator: int, denominator: int) -> int | None:
    """Return a ratio as a rounded integer percentage, or None when the denominator is zero."""
    return round(numerator / denominator * 100) if denominator else None


def _format_pct(numerator: int, denominator: int) -> str:
    """Format a ratio as a rounded percentage string, or a dash when the denominator is zero."""
    raw = _raw_pct(numerator, denominator)
    return f"{raw}%" if raw is not None else "—"


def _format_duration(td: timedelta | None) -> str:
    """Format a timedelta as a compact human-readable string, or a dash when None."""
    if td is None:
        return "—"
    total_seconds = int(td.total_seconds())
    if total_seconds < 0:
        return "—"
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _resolve_period(request, default: str = DEFAULT_PERIOD) -> tuple[str, date | None]:
    """Resolve the stateless ``?period=`` querystring to a ``(period_key, cutoff_date)`` pair.

    Falls back to ``default`` for a missing/invalid value — the personal console uses the module
    ``DEFAULT_PERIOD`` ("7d"), while the org ``ManagerLensView`` passes ``default="all"`` because
    org velocity is cumulative. ``cutoff_date`` is ``None`` for the "all time" period, today's date
    for the zero-day "today" period, and ``today - days`` otherwise. Shared by both console surfaces
    so they honour the same range semantics with no persisted state.
    """
    period = request.GET.get("period", default)
    if period not in PERIOD_DAYS:
        period = default
    days = PERIOD_DAYS[period]
    cutoff_date = localdate() - timedelta(days=days) if days is not None else None
    if days == 0:
        cutoff_date = localdate()
    return period, cutoff_date


def _shipped_metrics_for(user: User):
    """Return the ``MergeMetric`` queryset of merges opened by DAIV **for this user** (AR9 / AD-10).

    A merge counts as "shipped by DAIV, by this user" iff a ``Run`` in one of the user's own
    sessions shares the merge's ``(repo_id, merge_request_iid)`` — the value-based ``Exists`` join
    (there is no FK) *is* the attribution predicate, so human-authored MRs (no matching DAIV ``Run``)
    are excluded. Uses the literal ``session__user=user`` — never ``Run.objects.by_owner`` /
    ``visible_to``, which short-circuit to ``.all()`` for admins and would break personal scope (AC10).
    """
    daiv_user_runs = Run.objects.filter(
        session__user=user,
        merge_request_iid__isnull=False,
        repo_id=OuterRef("repo_id"),
        merge_request_iid=OuterRef("merge_request_iid"),
    )
    return MergeMetric.objects.filter(Exists(daiv_user_runs))


def _hero_breakdown_rows(shipped, user: User, cutoff_date: date | None) -> tuple[list, list]:
    """Annotate the exact ``MergeMetric`` rows behind the Hero counts (AC1/AC5, read-only).

    Derived from the SAME ``shipped`` base ``get_velocity_data`` aggregates, so the revealed list
    can never diverge from the number (NFR1): a ``.count()`` and this ``.annotate()`` over one base
    queryset return the identical set. Each row carries its native ``MergeMetric`` fields
    (``repo_id`` / ``merge_request_iid`` / ``title`` / ``merged_at``) plus two annotation-only names
    joined from the most-recent attributing ``Run`` in one of the USER's own sessions:

    - ``web_url`` — the already-persisted ``Run.merge_request_web_url`` (no live client, AC6/AC11);
      may be ``""`` (blank, best-effort) → the template degrades to the session link.
    - ``thread_id`` — the representative session for the drill-through.

    The correlation is the literal ``session__user=user`` scope — never ``by_owner`` / ``visible_to``
    (which broaden to ``.all()`` for admins, AC7). The this-range list applies the IDENTICAL window
    filter ``get_velocity_data`` uses (``merged_at__date__gte=cutoff_date`` when dated; unfiltered for
    "all"), so it matches the headline count; the unfiltered list matches the odometer (AC8).
    """
    attributing_run = Run.objects.filter(
        session__user=user, repo_id=OuterRef("repo_id"), merge_request_iid=OuterRef("merge_request_iid")
    ).order_by("-created_at", "-pk")  # ``-pk`` is a deterministic tiebreak on a created_at collision
    rows = shipped.annotate(
        # Prefer an attributing run that actually PERSISTED a url: a blank best-effort url on the
        # newest re-run must not hide a good url carried by an older run (AC11 degrades to the session
        # link only when NO attributing run has one). ``thread_id`` (the session PK) is always present.
        web_url=Subquery(attributing_run.exclude(merge_request_web_url="").values("merge_request_web_url")[:1]),
        thread_id=Subquery(attributing_run.values("session__thread_id")[:1]),
    ).order_by("-merged_at")
    this_range_rows = rows.filter(merged_at__date__gte=cutoff_date) if cutoff_date is not None else rows
    return list(this_range_rows), list(rows)


def get_velocity_data(cutoff_date: date | None, queryset=None) -> dict | None:
    """Aggregate code-velocity + DAIV-attribution over a ``MergeMetric`` queryset.

    The single shared counting body (AC11): the Manager Lens passes nothing (defaulting to the
    org-wide ``MergeMetric.objects.all()``, never derived from ``RunEnvelope`` per AD-10), while the
    personal Hero passes the user-scoped ``_shipped_metrics_for(user)`` queryset — so the org number
    and the personal number can never disagree in method. An optional ``cutoff_date`` restricts to
    merges on/after that date. Returns ``None`` when there are zero matching rows so the caller can
    render an honest cold-load state instead of a misleading zero reading.
    """
    merges = MergeMetric.objects.all() if queryset is None else queryset
    if cutoff_date is not None:
        merges = merges.filter(merged_at__date__gte=cutoff_date)

    stats = merges.aggregate(
        total=Count("id"),
        total_added=Sum("lines_added", default=0),
        total_removed=Sum("lines_removed", default=0),
        daiv_merges=Count("id", filter=Q(daiv_commits__gt=0)),
        total_commits_sum=Sum("total_commits", default=0),
        daiv_commits_sum=Sum("daiv_commits", default=0),
    )

    if not stats["total"]:
        return None

    total_merges = stats["total"]
    daiv_merges = stats["daiv_merges"]
    human_merges = total_merges - daiv_merges
    total_commits = stats["total_commits_sum"]
    daiv_commits = stats["daiv_commits_sum"]
    human_commits = max(0, total_commits - daiv_commits)
    max_lines = max(stats["total_added"], stats["total_removed"], 1)

    return {
        "total_merges": total_merges,
        "lines_added": stats["total_added"],
        "lines_removed": stats["total_removed"],
        "net_lines": stats["total_added"] - stats["total_removed"],
        "daiv_merges_pct": _format_pct(daiv_merges, total_merges),
        "daiv_merges_pct_raw": min(_raw_pct(daiv_merges, total_merges) or 0, 100),
        "daiv_merges": daiv_merges,
        "human_merges": human_merges,
        "daiv_commits_pct": _format_pct(daiv_commits, total_commits),
        "daiv_commits_pct_raw": min(_raw_pct(daiv_commits, total_commits) or 0, 100),
        "daiv_commits": daiv_commits,
        "human_commits": human_commits,
        "lines_added_pct": round(stats["total_added"] / max_lines * 100, 1),
        "lines_removed_pct": round(stats["total_removed"] / max_lines * 100, 1),
    }


# The console Feed lists the user's RUN_FEED rows newest-first; a single page (mirrors the
# notifications ``paginate_by``). Render content comes from each run's live RunEnvelope.
FEED_PAGE_SIZE = 20

# Per-status accent applied inline as ``var(--color-status-*)`` — the status utility classes are
# not all compiled and the Feed reuses existing tokens without a Tailwind rebuild. Classifying has
# no accent (neutral/dashed). Keyed on the hyphenated ``EnvelopeStatus`` values.
_FEED_ACCENT_VARS = {
    EnvelopeStatus.ALL_CLEAR: "--color-status-clear",
    EnvelopeStatus.FOUND_ISSUES: "--color-status-found",
    EnvelopeStatus.NEEDS_ATTENTION: "--color-status-attn",
    EnvelopeStatus.FAILED: "--color-status-fail",
}


def build_feed_item(run: Run, notification: Notification | None, envelope: RunEnvelope | None) -> dict:
    """Assemble the render-ready Feed item for a run from its (already-resolved) envelope.

    ``envelope is None`` is the "classifying…" state (the classifier has not written the envelope
    yet — there is no ``pending`` row/status). The caller resolves the envelope and passes it in:
    the dashboard batches them in one query (avoids an N+1), and ``FeedItemView`` resolves the one
    run's envelope via ``RunEnvelope.objects.for_run``. Reads status/count/summary straight off the
    envelope (never recomputed). ``read_at`` is carried for Story 2.4 but is NOT rendered here (no
    unread delta / badge / mark-seen in Story 2.3).
    """
    if envelope is None:
        status_slug = "classifying"
        accent_var = ""
    else:
        status_slug = envelope.status
        accent_var = _FEED_ACCENT_VARS.get(envelope.status, "")
    return {
        "run": run,
        "envelope": envelope,
        "status_slug": status_slug,
        "accent_var": accent_var,
        "read_at": notification.read_at if notification is not None else None,
        "link_url": notification.link_url if notification is not None else "",
    }


class FeedItemView(LoginRequiredMixin, TemplateView):
    """Render a single Feed item (the SSE re-fetch source, Story 2.3 AC9).

    Owner-scoped: only renders if the requesting user holds a ``RUN_FEED`` row for that run
    (mirrors ``MarkNotificationReadView``'s owner-scoped guard) — else 404. The resolved partial
    omits the classifying/in-flight hooks, so the client stops streaming for that item.
    """

    template_name = "accounts/_feed_item.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        run_id = self.kwargs["run_id"]
        notification = (
            Notification.objects
            .filter(
                recipient=self.request.user,
                event_type=EventType.RUN_FEED,
                source_type="sessions.Run",
                source_id=str(run_id),
            )
            .order_by("-created")
            .first()
        )
        if notification is None:
            raise Http404
        run = Run.objects.filter(pk=run_id).first()
        if run is None:
            raise Http404
        ctx["item"] = build_feed_item(run, notification, RunEnvelope.objects.for_run(run))
        return ctx


@method_decorator(require_POST, name="dispatch")
class FeedItemSeenView(LoginRequiredMixin, TemplateView):
    """Mark the requester's own Feed row for a run seen, then re-render the seen item (Story 2.4).

    Owner-scoped exactly like ``FeedItemView``: only the requesting user's ``RUN_FEED`` row for that
    run is touched — a cross-user request, a run with no Feed row, or a deleted run all 404.
    ``mark_as_read`` is idempotent, so a repeat POST is a no-op. The ``HX-Trigger: feed:seen`` header
    pushes the persistent badge container to re-fetch its fragment (which re-reads the envelope-aware
    ``feed_unread_count``), announcing the change via its ``aria-live`` region.
    """

    template_name = "accounts/_feed_item.html"

    def post(self, request, run_id):
        notification = (
            Notification.objects
            .filter(
                recipient=request.user, event_type=EventType.RUN_FEED, source_type="sessions.Run", source_id=str(run_id)
            )
            .order_by("-created")
            .first()
        )
        if notification is None:
            raise Http404
        run = Run.objects.filter(pk=run_id).first()
        if run is None:
            raise Http404
        notification.mark_as_read()
        resp = self.render_to_response({"item": build_feed_item(run, notification, RunEnvelope.objects.for_run(run))})
        resp["HX-Trigger"] = "feed:seen"
        return resp


class FeedUnreadBadgeView(LoginRequiredMixin, TemplateView):
    """Render just the Feed unread badge fragment (the ``feed:seen`` live-refresh target).

    The count comes from the ``feed_unread_count`` context processor, so the fragment is correct
    both on a full console load and on every ``feed:seen`` re-fetch.
    """

    template_name = "accounts/_feed_unread_badge.html"


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/dashboard.html"

    def get_template_names(self) -> list[str]:
        # HTMX requests get just the console-body fragment (three region sections);
        # a normal GET renders the full shell. Mirrors ``SessionListView`` so later
        # stories' region partials refresh in place.
        if is_htmx(self.request):
            return ["accounts/_console_body.html"]
        return ["accounts/dashboard.html"]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        period, cutoff_date = _resolve_period(self.request)

        # Personal-by-default (AC1): the console default carries NO org/aggregate content for
        # anyone — admin OR member. Org velocity / total_users now live ONLY in the Manager Lens
        # (``ManagerLensView``); every read here is personal-scoped via ``visible_to`` / ``user=``.
        user = self.request.user
        context["activity"] = self._get_activity_data(cutoff_date, user)
        context["hero"] = self._get_hero_data(user, cutoff_date, period)
        context["active_api_keys"] = APIKey.objects.filter(user=user, revoked=False).count()
        context["periods"] = [{"key": key, "label": label} for key, label, _ in PERIOD_CHOICES]
        context["current_period"] = period
        context["active_schedules"] = ScheduledJob.objects.filter(user=user, is_enabled=True).count()
        feed = self._get_feed_data(user)
        reconciled = feed.pop("feed_reconciled")
        context.update(feed)
        # Reconciliation happens at render time (AD-6 live/cached read). Only stamp "last checked"
        # when a live MR-state read ACTUALLY occurred this render (>=1 actionable MR run) — a feed
        # with nothing to reconcile must not claim a source-of-truth check that never happened
        # (review P2 / NFR1). Read-only / presentation-only: passed into context, never stored.
        context["reconciled_at"] = timezone.now() if reconciled else None

        return context

    def _get_hero_data(self, user: User, cutoff_date: date | None, period: str) -> dict | None:
        """Compose the personal Throughput Hero from the user-scoped ``MergeMetric ⋈ Run`` join.

        Read-only (``.count()`` / ``.aggregate()`` only — AC12): nothing is written on the render
        path. Returns ``None`` when the user has shipped nothing all-time so the template renders the
        honest empty state rather than a ``0`` dressed as a headline fact (AC13); a genuine "0 this
        range" with a non-zero all-time is a true fact and *is* rendered.

        - ``this_range`` — the range-scoped count through the SHARED ``get_velocity_data`` body
          (never a forked count, AC11); ``0`` when the window is empty but all-time is not.
        - ``delta`` — ``this_range − prev_equal_window`` where the previous window spans the same
          number of days as the current [cutoff, today] window (``PERIOD_DAYS[period] + 1``);
          ``None`` for the "all time" range (no prior window) so the template hides the chip.
        - ``all_time`` — the range-invariant odometer count.
        - ``estimate`` — ``None`` in v1 (D1: no defensible dev-hours field exists); the key is kept
          so the template's ``{% if hero.estimate %}`` demotion path stays exercisable.
        """
        shipped = _shipped_metrics_for(user)
        all_time = shipped.count()
        if not all_time:
            return None

        this_range = get_velocity_data(cutoff_date, queryset=shipped)
        this_count = this_range["total_merges"] if this_range else 0

        delta = None
        if period != "all" and cutoff_date is not None:
            # The current window ``merged_at__date >= cutoff_date`` is [cutoff, today] *inclusive*,
            # spanning ``PERIOD_DAYS[period] + 1`` days ("7d" cutoff = today−7 ⇒ 8 days; "today" ⇒ 1).
            # The preceding window must span the SAME number of days, else a flat one-merge-per-day
            # cadence reports a spurious +1 (an 8-day current window vs a 7-day previous one).
            window_span = (PERIOD_DAYS[period] or 0) + 1
            prev_start = cutoff_date - timedelta(days=window_span)
            prev_count = shipped.filter(merged_at__date__gte=prev_start, merged_at__date__lt=cutoff_date).count()
            delta = this_count - prev_count

        # Story 3.2 — attach the exact underlying rows behind each number, derived from the SAME
        # ``shipped`` base with the identical window filter, so the click-through breakdown always
        # reconciles with the count (``len(this_range_rows) == this_count``, ``len(all_time_rows) ==
        # all_time`` by construction). Read-only annotate/list only — no live client (AC5/AC6/AC11).
        this_range_rows, all_time_rows = _hero_breakdown_rows(shipped, user, cutoff_date)

        return {
            "this_range": {"total_merges": this_count},
            "period": period,
            "delta": delta,
            "all_time": all_time,
            "estimate": None,
            "this_range_rows": this_range_rows,
            "all_time_rows": all_time_rows,
        }

    def _get_feed_data(self, user: User) -> dict:
        """Build the console Feed: RUN_FEED rows newest-first, each rendered from its live envelope.

        Rows come from the Feed notifications (the per-user seen-state anchor); render content comes
        from the live Run + RunEnvelope. Envelopes for the rendered page are batched in one query
        (no N+1). The zero-state seal shows only when NOTHING across the user's whole feed needs
        attention — decided over the full set, not just the rendered page, so a finding paged past
        ``FEED_PAGE_SIZE`` is never hidden behind a false "nothing needs you." ``for_run`` is
        sync-only and ``DashboardView`` is sync, so direct calls are correct.
        """
        feed_qs = Notification.objects.filter(recipient=user, event_type=EventType.RUN_FEED)
        notifications = list(feed_qs.order_by("-created")[:FEED_PAGE_SIZE])
        run_ids = [n.source_id for n in notifications]
        runs_by_id = {str(pk): run for pk, run in Run.objects.filter(pk__in=run_ids).in_bulk().items()}
        # Batch the page's envelopes in one query rather than one ``for_run`` per row (N+1).
        envelopes_by_run = {str(env.run_id): env for env in RunEnvelope.objects.filter(run_id__in=run_ids)}

        items: list[dict] = []
        in_flight_ids: list[str] = []
        latest_finished = None
        feed_reconciled = False
        for notification in notifications:
            run = runs_by_id.get(notification.source_id)
            if run is None:
                # The run was deleted out from under a stale Feed row; skip it rather than 500.
                continue
            envelope = envelopes_by_run.get(str(run.id))
            # Reconcile against live MR state (Story 3.3, AC4/AC5). A run that WAS actionable but
            # whose MR resolved externally (merged/closed) leaves the surface — it no longer counts
            # toward attention nor renders as awaiting. An ``all-clear`` run (never actionable) is
            # NOT dropped; it stays as a quiet card, exactly as before. A read failure keeps the item
            # visible (AC6, fail-safe) since ``still_actionable`` then resolves to actionable.
            classification_actionable = envelope is None or envelope.is_actionable
            # A live MR-state read is performed iff the run is (classification-)actionable AND
            # references an MR (see reconcile.still_actionable). Track it so the view stamps
            # "last checked" only when a real reconciliation happened this render (review P2 / NFR1).
            if classification_actionable and run.merge_request_iid is not None:
                feed_reconciled = True
            if classification_actionable and not still_actionable(run, envelope):
                continue
            item = build_feed_item(run, notification, envelope=envelope)
            items.append(item)
            if item["status_slug"] == "classifying":
                in_flight_ids.append(str(run.id))
            if run.finished_at and (latest_finished is None or run.finished_at > latest_finished):
                latest_finished = run.finished_at

        # Seal decision. A run "needs attention" when it is classifying or non-all-clear. If any
        # rendered item already needs attention we render the list; otherwise confirm across the
        # WHOLE feed (not just this page) before sealing, so a finding paged past row 20 is never
        # masked by a false "nothing needs you." The full scan runs only in the all-clear case.
        page_has_attention = any(item["status_slug"] != EnvelopeStatus.ALL_CLEAR for item in items)
        if not items:
            has_attention, all_clear_count = False, 0
        elif page_has_attention:
            has_attention, all_clear_count = True, 0
        else:
            has_attention, all_clear_count = self._feed_attention_summary(feed_qs)

        zero = None
        if not has_attention:
            if items:
                zero = {
                    "variant": "audited-clean",
                    "all_clear_count": all_clear_count,
                    "last_checked": latest_finished,
                    "next_sweep": self._next_sweep(user),
                }
            else:
                zero = {"variant": "never-ran"}

        return {
            "feed_items": items,
            "feed_in_flight_ids": ",".join(in_flight_ids),
            "feed_has_attention": has_attention,
            "feed_zero": zero,
            "feed_reconciled": feed_reconciled,
        }

    @staticmethod
    def _feed_attention_summary(feed_qs) -> tuple[bool, int]:
        """Return ``(has_attention, all_clear_count)`` over the FULL feed set.

        ``has_attention`` routes every existing feed run through the shared ``still_actionable``
        predicate (Story 3.3, AC2) — a classifying run, a non-all-clear envelope, or an open/unknown
        live MR read all mean attention; an all-clear envelope or an externally-resolved MR does not
        — so the zero-state seal decision can never diverge from the badge or the per-item render.
        Called only when the rendered page is entirely quiet, to avoid a false seal that would hide a
        finding paged past ``FEED_PAGE_SIZE`` and to count all-clear runs across the whole feed.
        """
        run_ids = list(feed_qs.values_list("source_id", flat=True))
        if not run_ids:
            return False, 0
        runs_by_id = Run.objects.filter(pk__in=run_ids).in_bulk()
        envelopes_by_run = {str(env.run_id): env for env in RunEnvelope.objects.filter(run_id__in=run_ids)}
        all_clear_count = sum(1 for env in envelopes_by_run.values() if env.status == EnvelopeStatus.ALL_CLEAR)
        has_attention = any(still_actionable(run, envelopes_by_run.get(str(run.id))) for run in runs_by_id.values())
        return has_attention, all_clear_count

    @staticmethod
    def _next_sweep(user: User):
        """Earliest upcoming scheduled-run time across the user's enabled schedules, or None."""
        return (
            ScheduledJob.objects
            .filter(user=user, is_enabled=True, next_run_at__isnull=False)
            .order_by("next_run_at")
            .values_list("next_run_at", flat=True)
            .first()
        )

    def _get_activity_data(self, cutoff_date: date | None, user: User) -> dict:
        visible = Run.objects.visible_to(user)
        activities = visible.filter(created_at__date__gte=cutoff_date) if cutoff_date is not None else visible

        successful = Q(status=RunStatus.SUCCESSFUL)
        failed = Q(status=RunStatus.FAILED)
        issue_trigger = Q(trigger_type=SessionOrigin.ISSUE_WEBHOOK)
        mr_trigger = Q(trigger_type=SessionOrigin.MR_WEBHOOK)
        mcp_trigger = Q(trigger_type=SessionOrigin.MCP_JOB)
        schedule_trigger = Q(trigger_type=SessionOrigin.SCHEDULE)
        api_trigger = Q(trigger_type=SessionOrigin.API_JOB)
        chat_trigger = Q(trigger_type=SessionOrigin.CHAT)
        duration_expr = ExpressionWrapper(F("finished_at") - F("started_at"), output_field=DurationField())

        stats = activities.aggregate(
            total=Count("id"),
            successful=Count("id", filter=successful),
            failed_count=Count("id", filter=failed),
            issues=Count("id", filter=issue_trigger & ~failed),
            mrs=Count("id", filter=mr_trigger & ~failed),
            mcp_jobs=Count("id", filter=mcp_trigger & ~failed),
            scheduled=Count("id", filter=schedule_trigger & ~failed),
            api_jobs=Count("id", filter=api_trigger & ~failed),
            chat_jobs=Count("id", filter=chat_trigger & ~failed),
            code_changes=Count("id", filter=successful & Q(code_changes=True)),
            avg_duration=Avg(duration_expr, filter=successful),
        )

        # "Running now" is global (not period-filtered); share the request-memoized helper
        # with the ``nav`` context processor so the sidebar badge and dashboard card resolve
        # to a single query per render.
        running_count = running_jobs_count(self.request, user)

        total = stats["total"]
        successful_count = stats["successful"]
        failed_count = stats["failed_count"]
        issues_count = stats["issues"]
        mrs_count = stats["mrs"]
        mcp_jobs_count = stats["mcp_jobs"]
        scheduled_count = stats["scheduled"]
        api_jobs_count = stats["api_jobs"]
        chat_jobs_count = stats["chat_jobs"]
        sessions_url = reverse("session_list")

        # Non-overlapping segments for the breakdown bar.
        # Trigger types are mutually exclusive, so the trigger-keyed segments never overlap.
        # Each segment excludes failed; "Other" absorbs the remainder (e.g. UI jobs).
        other_count = max(
            0,
            total
            - issues_count
            - mrs_count
            - mcp_jobs_count
            - scheduled_count
            - api_jobs_count
            - chat_jobs_count
            - failed_count,
        )
        raw_segments = [
            ("Issues", issues_count, "bg-amber-500/50", f"{sessions_url}?trigger={SessionOrigin.ISSUE_WEBHOOK}"),
            ("MR/PR", mrs_count, "bg-cyan-500/50", f"{sessions_url}?trigger={SessionOrigin.MR_WEBHOOK}"),
            ("MCP Job", mcp_jobs_count, "bg-indigo-500/50", f"{sessions_url}?trigger={SessionOrigin.MCP_JOB}"),
            ("Scheduled", scheduled_count, "bg-violet-500/40", f"{sessions_url}?trigger={SessionOrigin.SCHEDULE}"),
            ("API", api_jobs_count, "bg-emerald-500/50", f"{sessions_url}?trigger={SessionOrigin.API_JOB}"),
            ("Chat", chat_jobs_count, "bg-sky-500/40", f"{sessions_url}?trigger={SessionOrigin.CHAT}"),
            ("Other", other_count, "bg-gray-500/30", None),
            ("Failed", failed_count, "bg-red-500/40", f"{sessions_url}?status={RunStatus.FAILED}"),
        ]
        segments = []
        for label, value, css, url in raw_segments:
            if value <= 0:
                continue
            segments.append({
                "label": label,
                "value": value,
                "pct": round(value / total * 100, 1) if total else 0,
                "css": css,
                "url": url,
            })

        code_changes_count = stats["code_changes"]

        return {
            "total": total,
            "running": running_count,
            "success_rate": _format_pct(successful_count, successful_count + failed_count),
            "success_rate_raw": _raw_pct(successful_count, successful_count + failed_count),
            "failed": failed_count,
            "code_changes": code_changes_count,
            "code_changes_pct": _format_pct(code_changes_count, successful_count),
            "avg_duration": _format_duration(stats["avg_duration"]),
            "activity_url": sessions_url,
            "segments": segments,
        }


class ManagerLensView(AdminRequiredMixin, TemplateView):
    """Admin-only org-impact surface — the relocated Code-Velocity / DAIV-attribution content.

    ``AdminRequiredMixin`` raises ``PermissionDenied`` (→ HTTP 403) for a logged-in non-admin, so a
    forged direct request is denied server-side, not merely hidden (AC6). Reuses the stateless
    ``?period=`` idiom and the shared module-level ``get_velocity_data`` computation (no duplicated
    query). Nothing about the current surface is persisted (AC4) — the Lens is simply a distinct
    route from the personal console, so a fresh landing is always ``/dashboard/``.
    """

    template_name = "accounts/manager_lens.html"

    def get_template_names(self) -> list[str]:
        # HTMX (the top-bar toggle) swaps in the body fragment; a normal GET / no-JS deep link
        # renders the full shell. Mirrors ``SessionListView`` / ``DashboardView``.
        if is_htmx(self.request):
            return ["accounts/_manager_lens.html"]
        return ["accounts/manager_lens.html"]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        _, cutoff_date = _resolve_period(self.request, default="all")

        # Org-scoped by design — this is the ONE place org velocity / total_users render. The Lens
        # defaults to the cumulative "all" window (not the personal console's "today"): org velocity
        # is cumulative, so a fresh visit shows real historical impact instead of an empty view.
        # ``get_velocity_data`` then returns ``None`` only for a genuinely-empty org (zero
        # ``MergeMetric`` rows ever) — the honest cold-load skeleton, never a "DAIV did nothing"
        # zero (AC5). No range switcher on the Lens yet — that arrives with Epic 3.
        context["velocity"] = get_velocity_data(cutoff_date)
        context["total_users"] = User.objects.count()

        return context


class APIKeyListView(LoginRequiredMixin, ListView):
    model = APIKey
    template_name = "accounts/api_keys.html"
    context_object_name = "api_keys"
    paginate_by = 25

    def get_queryset(self):
        qs = super().get_queryset().order_by("revoked", "-created")
        if self.request.user.is_admin:
            return qs.select_related("user")
        return qs.filter(user=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["new_key"] = self.request.session.pop("new_api_key", None)
        context["form"] = APIKeyCreateForm()
        context["is_admin"] = self.request.user.is_admin
        return context


class APIKeyCreateView(LoginRequiredMixin, View):
    def post(self, request):
        form = APIKeyCreateForm(request.POST)
        if not form.is_valid():
            for error in form.errors.values():
                messages.error(request, error[0])
            return redirect("api_keys")

        try:
            key_generator = APIKey.objects.key_generator
            key, prefix, hashed_key = key_generator.generate()
            APIKey.objects.create(
                user=request.user, name=form.cleaned_data["name"], prefix=prefix, hashed_key=hashed_key
            )
        except IntegrityError:
            messages.error(request, "Failed to create API key due to a conflict. Please try again.")
            return redirect("api_keys")
        except Exception:
            logger.exception("Unexpected error creating API key for user %s", request.user.pk)
            messages.error(request, "An unexpected error occurred. Please try again.")
            return redirect("api_keys")

        request.session["new_api_key"] = key
        messages.success(request, f"API key '{form.cleaned_data['name']}' created.")
        return redirect("api_keys")


class APIKeyRevokeView(LoginRequiredMixin, View):
    def post(self, request, pk):
        qs = APIKey.objects.all() if request.user.is_admin else APIKey.objects.filter(user=request.user)
        api_key = qs.filter(pk=pk).first()
        if api_key is None:
            messages.error(request, "API key not found.")
        elif api_key.revoked:
            messages.info(request, f"API key '{api_key.name}' was already revoked.")
        else:
            api_key.revoked = True
            api_key.save(update_fields=["revoked"])
            messages.success(request, f"API key '{api_key.name}' revoked.")
        return redirect("api_keys")


# ---------------------------------------------------------------------------
# User management views (admin only)
# ---------------------------------------------------------------------------


class UserListView(AdminRequiredMixin, FilterView):
    model = User
    filterset_class = UserFilter
    template_name = "accounts/users.html"
    context_object_name = "users"
    ordering = ["-date_joined"]
    paginate_by = 25
    # Invalid URL params (e.g. ?role=bogus) drop silently instead of blanking the list.
    strict = False

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context["filter"].form
        cleaned = form.cleaned_data if form.is_valid() else {}
        context["search_query"] = cleaned.get("q") or ""
        context["current_role"] = cleaned.get("role") or ""
        return context


class UserCreateView(BreadcrumbMixin, AdminRequiredMixin, CreateView):
    model = User
    form_class = UserCreateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("user_list")

    def form_valid(self, form):
        response = super().form_valid(form)
        assert isinstance(self.object, User)
        login_url = self.request.build_absolute_uri(reverse("account_login"))
        email_sent = send_welcome_email(self.object, login_url)
        if email_sent:
            messages.success(self.request, f"User '{self.object.email}' created. A welcome email has been sent.")
        else:
            messages.warning(
                self.request,
                f"User '{self.object.email}' created, but the welcome email could not be sent."
                " Please notify the user manually.",
            )
        return response

    def get_breadcrumbs(self):
        return [{"label": "Users", "url": reverse("user_list")}, {"label": "New user", "url": None}]


class UserUpdateView(BreadcrumbMixin, SuccessMessageMixin, AdminRequiredMixin, UpdateView):
    model = User
    form_class = UserUpdateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("user_list")
    success_message = "User '%(email)s' updated."

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["requesting_user"] = self.request.user
        return kwargs

    def get_breadcrumbs(self):
        return [{"label": "Users", "url": reverse("user_list")}, {"label": self.object.email, "url": None}]


class UserDeleteView(BreadcrumbMixin, SuccessMessageMixin, AdminRequiredMixin, DeleteView):
    model = User
    template_name = "accounts/user_confirm_delete.html"
    success_url = reverse_lazy("user_list")
    success_message = "User deleted."

    def post(self, request, *args, **kwargs):
        user = self.get_object()
        if user.pk == request.user.pk:
            messages.error(request, "You cannot delete your own account.")
            return redirect("user_list")
        if user.is_last_active_admin():
            messages.error(request, "Cannot delete the last admin. Promote another user first.")
            return redirect("user_list")
        return super().post(request, *args, **kwargs)

    def get_success_message(self, cleaned_data: dict) -> str:
        return f"User '{self.object.email}' deleted."

    def get_breadcrumbs(self):
        return [
            {"label": "Users", "url": reverse("user_list")},
            {"label": self.object.email, "url": reverse("user_update", args=[self.object.pk])},
            {"label": "Delete", "url": None},
        ]
