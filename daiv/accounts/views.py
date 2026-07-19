import logging
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db import IntegrityError
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q, Sum
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.timezone import localdate
from django.views import View
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, ListView, TemplateView, UpdateView

from django_filters.views import FilterView
from notifications.choices import EventType
from notifications.models import Notification
from sessions.models import EnvelopeStatus, Run, RunEnvelope, RunStatus, SessionOrigin

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
DEFAULT_PERIOD = "today"


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
    ``DEFAULT_PERIOD`` ("today"), while the org ``ManagerLensView`` passes ``default="all"`` because
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


def get_velocity_data(cutoff_date: date | None) -> dict | None:
    """Aggregate org-wide code-velocity + DAIV-attribution from ``MergeMetric``.

    Org-wide by design — ``MergeMetric.objects.all()`` (never user-scoped, never derived from
    ``RunEnvelope`` per AD-10); an optional ``cutoff_date`` restricts to merges on/after that date.
    Returns ``None`` when there are zero matching rows so the caller can render an honest cold-load
    state instead of a misleading zero reading. Single source of truth for the Manager Lens.
    """
    merges = MergeMetric.objects.all()
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
        context["active_api_keys"] = APIKey.objects.filter(user=user, revoked=False).count()
        context["periods"] = [{"key": key, "label": label} for key, label, _ in PERIOD_CHOICES]
        context["current_period"] = period
        context["active_schedules"] = ScheduledJob.objects.filter(user=user, is_enabled=True).count()
        context.update(self._get_feed_data(user))

        return context

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
        for notification in notifications:
            run = runs_by_id.get(notification.source_id)
            if run is None:
                # The run was deleted out from under a stale Feed row; skip it rather than 500.
                continue
            item = build_feed_item(run, notification, envelope=envelopes_by_run.get(str(run.id)))
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
        }

    @staticmethod
    def _feed_attention_summary(feed_qs) -> tuple[bool, int]:
        """Return ``(has_attention, all_clear_count)`` over the FULL feed set.

        ``has_attention`` is True when any feed run is classifying (no envelope yet) or has a
        non-all-clear envelope. Called only when the rendered page is entirely all-clear, to avoid a
        false zero-state seal that would hide a finding paged past ``FEED_PAGE_SIZE`` and to count
        all-clear runs across the whole feed (not just the page).
        """
        run_ids = list(feed_qs.values_list("source_id", flat=True))
        if not run_ids:
            return False, 0
        statuses = list(RunEnvelope.objects.filter(run_id__in=run_ids).values_list("status", flat=True))
        all_clear_count = sum(1 for status in statuses if status == EnvelopeStatus.ALL_CLEAR)
        # Fewer envelopes than feed runs ⇒ at least one run is still classifying (no envelope) — don't seal.
        classifying = len(statuses) < len(run_ids)
        has_non_all_clear = any(status != EnvelopeStatus.ALL_CLEAR for status in statuses)
        return (classifying or has_non_all_clear), all_clear_count

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
