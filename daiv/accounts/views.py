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
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, ListView, TemplateView, UpdateView

from django_filters.views import FilterView
from notifications.choices import EventType
from notifications.models import Notification
from sandbox_envs.services import resolve_repo_envs
from sessions.models import EnvelopeStatus, OfferedAction, Run, RunEnvelope, RunStatus, SessionOrigin
from sessions.queue import QUEUE_DECAY_STALE_AFTER, impact_class, order_queue
from sessions.reconcile import still_actionable
from sessions.services import RepoTarget, submit_batch_runs

from accounts.context_processors import running_jobs_count
from accounts.emails import send_welcome_email
from accounts.filters import UserFilter
from accounts.forms import APIKeyCreateForm, UserCreateForm, UserUpdateForm
from accounts.mixins import AdminRequiredMixin, BreadcrumbMixin
from accounts.models import APIKey, User
from codebase.authorization import RepositoryAccessDenied, can_run
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


def _fixable_actionable(run: Run, envelope: RunEnvelope | None) -> list[dict]:
    """The fix-able ``actionable[]`` subset for a run — items carrying a non-empty ``fix_prompt``.

    The single Finding -> Fix gate (Story 5.1): items are returned ONLY when the run's live envelope
    offers ``FIX`` **and** the run is still ``still_actionable`` (the shared predicate — never
    re-derived inline). Empty list for every non-fixable run, so the template stays dumb (render the
    ``fix it`` affordance iff this list is non-empty). Read-only: ``still_actionable`` may perform a
    *cached* live MR read for an MR-linked run, but this never writes and enqueues nothing.
    """
    if envelope is None or envelope.offered_action != OfferedAction.FIX:
        return []
    if not still_actionable(run, envelope):
        return []
    return [item for item in envelope.actionable if item.get("fix_prompt", "").strip()]


def compose_fix_prompt(fixable: list[dict]) -> str:
    """Compose the ONE fix prompt from a run's fix-able items (UX-DR8: one fix per run).

    A single item is used verbatim; multiple items are concatenated as a numbered list so the single
    launched change session addresses every flagged finding. The text is the classifier's stored
    (already-stripped) ``fix_prompt`` — inert, untrusted seed/display text, never executed nor used
    as a template with user context.
    """
    prompts = [item["fix_prompt"].strip() for item in fixable]
    if len(prompts) == 1:
        return prompts[0]
    return "\n\n".join(f"{index}. {prompt}" for index, prompt in enumerate(prompts, start=1))


def build_feed_item(run: Run, notification: Notification | None, envelope: RunEnvelope | None) -> dict:
    """Assemble the render-ready Feed item for a run from its (already-resolved) envelope.

    ``envelope is None`` is the "classifying…" state (the classifier has not written the envelope
    yet — there is no ``pending`` row/status). The caller resolves the envelope and passes it in:
    the dashboard batches them in one query (avoids an N+1), and ``FeedItemView`` resolves the one
    run's envelope via ``RunEnvelope.objects.for_run``. Reads status/count/summary straight off the
    envelope (never recomputed). ``read_at`` is carried for Story 2.4 but is NOT rendered here (no
    unread delta / badge / mark-seen in Story 2.3).

    Story 5.1 attaches ``offered_action`` + a filtered ``fixable`` subset so the template can offer a
    ``fix it`` affordance without re-deriving actionability: the gate (``offered_action == FIX`` +
    ``still_actionable`` + a non-empty ``fix_prompt``) lives in ``_fixable_actionable``. Computed on
    EVERY call site (dashboard loop, ``FeedItemView`` re-fetch, ``FeedItemSeenView``) so the single-
    item render paths gate identically — never a stale ``fix it`` on an externally-resolved finding.

    Story 5.2 attaches ``merge_request_web_url`` (the ``review this`` link-out target), ``can_rerun``
    (``run.session.scheduled_job`` resolvable — the low-emphasis re-run secondary control's gate), and
    mirrors the Queue's ``is_retryable`` downgrade so a domain-forbidden retry (webhook/chat/
    non-terminal origin) never surfaces a ``retry`` verb the endpoint would reject.
    """
    if envelope is None:
        status_slug = "classifying"
        accent_var = ""
        offered_action = OfferedAction.NONE
    else:
        status_slug = envelope.status
        accent_var = _FEED_ACCENT_VARS.get(envelope.status, "")
        offered_action = envelope.offered_action
    # Mirror ``_build_queue_item``'s RETRY -> NONE downgrade: a RETRY offer the domain forbids
    # (``run.is_retryable`` False) or one the shared liveness predicate no longer holds for
    # (``still_actionable`` False — e.g. an externally-resolved MR) must never render a retry
    # affordance on the Feed either.
    if offered_action == OfferedAction.RETRY and (not run.is_retryable or not still_actionable(run, envelope)):
        offered_action = OfferedAction.NONE
    return {
        "run": run,
        "envelope": envelope,
        "status_slug": status_slug,
        "accent_var": accent_var,
        "offered_action": offered_action,
        "fixable": _fixable_actionable(run, envelope),
        "merge_request_web_url": run.merge_request_web_url,
        # A schedule-owned run can be re-run in place (Story 5.2). Independent of envelope status —
        # a secondary control, NOT an ``OfferedAction``. Requires a SCHEDULE-origin run (not merely a
        # session that happens to carry a ``scheduled_job``); ``scheduled_job_id`` avoids loading the FK.
        # Gated on the VIEWER OWNING the schedule: a scheduled run's ``session.user`` is the schedule
        # owner (the cron dispatcher submits as ``schedule.user``), so the render gate must match
        # ``FeedItemRerunView._resolve_schedule``'s owner check. Otherwise a schedule SUBSCRIBER holding
        # a ``RUN_FEED`` row for someone else's scheduled run would see a re-run button every click of
        # which 404s (the endpoint is owner-only) — a dead control (render gate != action gate).
        "can_rerun": (
            run.trigger_type == SessionOrigin.SCHEDULE
            and run.session.scheduled_job_id is not None
            and notification is not None
            and run.session.user_id == notification.recipient_id
        ),
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
        run = Run.objects.filter(pk=run_id).select_related("session").first()
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
        run = Run.objects.filter(pk=run_id).select_related("session").first()
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


# The DOM surface a launch action was invoked from — controls which item region the confirm swaps
# and which trigger button focus returns to. Server-validated (a client cannot inject an arbitrary
# id). Shared by every launch action (fix / retry / re-run).
_FIX_SURFACES = ("feed", "queue")
_DEFAULT_FIX_SURFACE = "feed"


class _LaunchActionMixin:
    """Shared owner-scope / surface / inline-notice primitives for the console's launch actions.

    Story 5.1's ``FeedItemFixView`` established these three helpers; Story 5.2 reuses them VERBATIM
    for ``retry`` and ``re-run`` rather than forking them, so every launch action resolves its target,
    validates its surface, and renders its inline error identically (and ``FeedItemFixView``'s
    behaviour is unchanged — it simply inherits what it used to define).
    """

    def _resolve_run(self, request, run_id) -> Run:
        """Owner-scope the run to the requester, else ``Http404``.

        Two owner paths, both literal-scoped (NEVER ``by_owner`` / ``visible_to``, which short-circuit
        to ``.all()`` for admins): a ``Run`` in one of the requester's own sessions (``session__user``),
        or a run the requester holds a ``RUN_FEED`` notification for (a schedule subscriber can act on
        a finding surfaced to them).
        """
        run = Run.objects.filter(pk=run_id, session__user=request.user).select_related("session").first()
        if run is not None:
            return run
        has_feed_row = Notification.objects.filter(
            recipient=request.user, event_type=EventType.RUN_FEED, source_type="sessions.Run", source_id=str(run_id)
        ).exists()
        if has_feed_row:
            run = Run.objects.filter(pk=run_id).select_related("session").first()
            if run is not None:
                return run
        raise Http404

    def _surface(self, request) -> str:
        surface = request.GET.get("surface")
        if surface is None:
            return _DEFAULT_FIX_SURFACE
        # A view may narrow the allow-list (e.g. re-run is Feed-only): a surface outside it is a
        # tampered request — reject it rather than silently coercing to the default (which would
        # mis-target the confirm swap to a nonexistent / wrong region).
        allowed = getattr(self, "_allowed_surfaces", _FIX_SURFACES)
        if surface not in allowed:
            raise Http404
        return surface

    @staticmethod
    def _dialog_notice(request, message):
        """Render a calm inline notice retargeted INTO the open dialog (no launch, no region swap)."""
        resp = render(request, "accounts/_fix_notice.html", {"message": message})
        resp["HX-Retarget"] = "#fix-preview-error"
        resp["HX-Reswap"] = "innerHTML"
        return resp


class FeedItemFixView(_LaunchActionMixin, LoginRequiredMixin, TemplateView):
    """Story 5.1 — the console's single launch capability AND the human-in-the-loop security gate.

    ``fix_prompt`` is classifier-authored from untrusted run prose, so no run is ever enqueued
    without an explicit POST following an explicit user confirm:

    - ``get()`` renders the scope/intent preview (target repo/ref + the finding intent + the inert,
      auto-escaped ``fix_prompt`` — never a generated diff). It is a **pure read**: it re-reads the
      live envelope, re-asserts the fix gate, and performs ZERO writes and enqueues ZERO runs.
    - ``post()`` re-validates server-side, enforces ``can_run``, resolves the sandbox env, and
      launches EXACTLY ONE change session via ``submit_batch_runs(trigger_type=UI_JOB)`` built from
      the ORIGINATING run's ``repo_id``/``ref`` — never the finding's ``actionable[].ref`` — then
      swaps the item region to a calm "fix started" state and fires ``HX-Trigger: fix:started``.

    Owner-scoped to the requester (a ``Run`` in one of their own sessions OR a run they hold a
    ``RUN_FEED`` notification for); cross-user / unknown → ``Http404``. NEVER ``by_owner`` /
    ``visible_to`` (they short-circuit to ``.all()`` for admins).
    """

    template_name = "accounts/_fix_preview.html"

    def get(self, request, *args, **kwargs):
        run = self._resolve_run(request, kwargs["run_id"])
        envelope = RunEnvelope.objects.for_run(run)
        fixable = _fixable_actionable(run, envelope)
        surface = self._surface(request)
        # Pure read only — no write, no enqueue. A stale/no-longer-fixable finding renders the inert
        # "no longer actionable" preview (``fixable`` empty) rather than a launchable dialog.
        return self.render_to_response({
            "run": run,
            "fixable": fixable,
            "fix_prompt": compose_fix_prompt(fixable) if fixable else "",
            "surface": surface,
            "region_id": f"{surface}-item-{run.id}",
            "trigger_id": f"fix-btn-{surface}-{run.id}",
        })

    def post(self, request, *args, **kwargs):
        run = self._resolve_run(request, kwargs["run_id"])
        envelope = RunEnvelope.objects.for_run(run)
        # Re-validate the LIVE envelope server-side and pull ``fix_prompt`` from it keyed by
        # (run_id, actionable id) — a client-supplied prompt field is never trusted.
        fixable = _fixable_actionable(run, envelope)
        surface = self._surface(request)
        region_id = f"{surface}-item-{run.id}"
        if not fixable:
            # Stale / tampered: the live envelope no longer offers FIX (or the item lost its
            # fix_prompt). No launch — a calm inline no-op inside the open dialog.
            return self._dialog_notice(request, _("This finding is no longer actionable — nothing was started."))

        # Everything that can raise on a hostile/stale payload or a revoked repo lives inside the
        # try, so a failure degrades to a calm inline notice ("no crash") rather than a 500: the
        # actionable-id/prompt assembly (an off-contract envelope item could lack ``id``), the
        # ``can_run`` gate (a repo-client error can raise), env resolution, and the launch itself.
        try:
            actionable_ids = [item["id"] for item in fixable]
            prompt = compose_fix_prompt(fixable)
            # Access can be revoked between render and submit — this pre-check and the
            # ``RepositoryAccessDenied`` catch below surface the same clean inline error.
            if not can_run(request.user, run.repo_id):
                return self._dialog_notice(request, _("Repository not found or not accessible."))
            repos = resolve_repo_envs(
                user=request.user, repos=[RepoTarget(repo_id=run.repo_id, ref=run.ref)], explicit_env_id=None
            )
            result = submit_batch_runs(user=request.user, prompt=prompt, repos=repos, trigger_type=SessionOrigin.UI_JOB)
        except RepositoryAccessDenied:
            return self._dialog_notice(request, _("Repository not found or not accessible."))
        except Exception:
            logger.exception(
                "finding_fix: launch failed for run=%s repo=%s user=%s", run.pk, run.repo_id, request.user.pk
            )
            return self._dialog_notice(request, _("Could not start the fix. Please try again in a moment."))

        if not result.runs:
            # Every repo target failed to enqueue — for this single-target launch that means NOTHING
            # started. Surface a calm inline error and fire NO ``fix:started``; the finding stays
            # actionable so the user can retry (a false "fix started" + dead batch link is worse).
            logger.warning(
                "finding_fix.launch_failed run_id=%s repo_id=%s user=%s errors=%s",
                run.pk,
                run.repo_id,
                request.user.pk,
                [failure.error for failure in result.failed],
            )
            return self._dialog_notice(request, _("Could not start the fix. Please try again in a moment."))

        # Traceability finding -> spawned batch (Q3 lightweight, no migration): the batch_id is
        # surfaced in the response AND logged bound to (origin run_id, actionable ids).
        logger.info(
            "finding_fix.launched run_id=%s actionable_ids=%s batch_id=%s repo_id=%s user=%s",
            run.pk,
            actionable_ids,
            result.batch_id,
            run.repo_id,
            request.user.pk,
        )
        resp = render(request, "accounts/_fix_started.html", {"region_id": region_id, "batch_id": result.batch_id})
        resp["HX-Trigger"] = "fix:started"
        return resp


class FeedItemRetryView(_LaunchActionMixin, LoginRequiredMixin, TemplateView):
    """Story 5.2 — retry a failed run in place (a lightweight confirm, then EXACTLY ONE launch).

    Retry replays the ORIGINATING run's own trusted ``prompt`` on its own ``repo_id``/``ref`` via
    ``submit_batch_runs(trigger_type=UI_JOB)`` — never a finding's ``actionable[].ref``. Unlike
    ``fix``, there is no untrusted classifier ``fix_prompt`` to review, so the confirm dialog is a
    plain "are you sure?" pause (double-launch guard + a11y), not a security gate.

    - ``get()`` renders the confirm dialog. Pure read: re-reads the live envelope, re-asserts the
      retry gate, ZERO writes / ZERO enqueues.
    - ``post()`` re-validates server-side (retry still offered + ``can_run``), launches one run, swaps
      the item region to a calm "retry started" state, and fires ``HX-Trigger: retry:started``.

    Owner-scoped exactly like ``FeedItemFixView`` (shared ``_LaunchActionMixin``).
    """

    template_name = "accounts/_fix_preview.html"

    @staticmethod
    def _retry_offered(run: Run, envelope: RunEnvelope | None) -> bool:
        """Whether a live retry is genuinely offered for this run (the single gate, GET and POST).

        RETRY is offered iff the run's live state is ``failed`` (a FAILED envelope, or — the common
        no-envelope case — a FAILED run status), the domain permits a retry (``run.is_retryable`` is
        False for webhook/chat/non-terminal origins), and the shared ``still_actionable`` predicate
        still holds. ``still_actionable`` is the ONLY liveness check — never re-derived inline.
        """
        if envelope is not None:
            status_failed = envelope.status == EnvelopeStatus.FAILED
        else:
            status_failed = run.status == RunStatus.FAILED
        return status_failed and run.is_retryable and still_actionable(run, envelope)

    def get(self, request, *args, **kwargs):
        run = self._resolve_run(request, kwargs["run_id"])
        envelope = RunEnvelope.objects.for_run(run)
        surface = self._surface(request)
        # Pure read only — no write, no enqueue. A stale/no-longer-retryable run renders the inert
        # "no longer actionable" dialog (``can_confirm`` False) rather than a launchable one.
        return self.render_to_response({
            "run": run,
            "fixable": [],
            "surface": surface,
            "region_id": f"{surface}-item-{run.id}",
            "trigger_id": f"retry-btn-{surface}-{run.id}",
            "can_confirm": self._retry_offered(run, envelope),
            "verb": "retry",
            "dialog_title": _("Retry this run?"),
            "confirm_label": _("Retry"),
            "stale_message": _("This run can no longer be retried — nothing to start."),
            # Retry re-launches the ORIGINATING run's single repo/ref — a one-entry scope.
            "scope_repos": [{"repo_id": run.repo_id, "ref": run.ref}],
            "post_url": reverse("feed_item_retry", kwargs={"run_id": run.id}) + f"?surface={surface}",
        })

    def post(self, request, *args, **kwargs):
        run = self._resolve_run(request, kwargs["run_id"])
        envelope = RunEnvelope.objects.for_run(run)
        surface = self._surface(request)
        region_id = f"{surface}-item-{run.id}"
        if not self._retry_offered(run, envelope):
            # Stale / tampered: the live state no longer offers retry. No launch — a calm no-op.
            return self._dialog_notice(request, _("This run can no longer be retried — nothing was started."))
        try:
            # Access can be revoked between render and submit; this pre-check and the
            # ``RepositoryAccessDenied`` catch below surface the same clean inline error. Kept INSIDE
            # the ``try`` (like ``FeedItemFixView``/``FeedItemRerunView``) so a repo-client or backstop
            # error raised inside ``can_run`` degrades to a calm inline notice, never an uncaught 500
            # that leaves the confirm dialog spinner-locked.
            if not can_run(request.user, run.repo_id):
                return self._dialog_notice(request, _("Repository not found or not accessible."))
            repos = resolve_repo_envs(
                user=request.user, repos=[RepoTarget(repo_id=run.repo_id, ref=run.ref)], explicit_env_id=None
            )
            # Seed from the run's OWN trusted prompt/repo/ref — NEVER a finding's actionable[].ref.
            result = submit_batch_runs(
                user=request.user, prompt=run.prompt, repos=repos, trigger_type=SessionOrigin.UI_JOB
            )
        except RepositoryAccessDenied:
            return self._dialog_notice(request, _("Repository not found or not accessible."))
        except Exception:
            logger.exception(
                "run_retry: launch failed for run=%s repo=%s user=%s", run.pk, run.repo_id, request.user.pk
            )
            return self._dialog_notice(request, _("Could not start the run. Please try again in a moment."))

        if not result.runs:
            # The only target failed to enqueue → nothing started. No false "started" + dead batch
            # link: fire no trigger and surface a calm inline error; the run stays retryable.
            logger.warning(
                "run_retry.launch_failed run_id=%s repo_id=%s user=%s errors=%s",
                run.pk,
                run.repo_id,
                request.user.pk,
                [failure.error for failure in result.failed],
            )
            return self._dialog_notice(request, _("Could not start the run. Please try again in a moment."))

        logger.info(
            "run_retry.launched run_id=%s batch_id=%s repo_id=%s user=%s",
            run.pk,
            result.batch_id,
            run.repo_id,
            request.user.pk,
        )
        resp = render(
            request,
            "accounts/_launch_started.html",
            {"region_id": region_id, "batch_id": result.batch_id, "started_label": _("Retry started…")},
        )
        resp["HX-Trigger"] = "retry:started"
        return resp


class FeedItemRerunView(_LaunchActionMixin, LoginRequiredMixin, TemplateView):
    """Story 5.2 — re-run the schedule behind a scheduled-run Feed item, in place.

    Re-run is a secondary control on scheduled-run Feed items (NOT an ``OfferedAction``, NOT on Queue
    rows). It replicates ``ScheduleRunNowView``'s exact service sequence — repos from
    ``schedule.repos``, envs resolved against the schedule OWNER, one launch via
    ``submit_batch_runs(trigger_type=SCHEDULE, scheduled_job=<job>)`` — but returns the calm-partial
    console idiom (a targeted region swap + ``HX-Trigger: rerun:started``) instead of a redirect.

    Owner-scoped to the requester via the shared ``_LaunchActionMixin``; the run must resolve to a
    non-null ``run.session.scheduled_job`` AND the requester must OWN that schedule
    (``schedule.user_id == request.user.id``) — merely holding a ``RUN_FEED`` notification for the run
    is NOT enough (else ``Http404``). This closes the subscriber-escalation the shared ``_resolve_run``
    would otherwise allow: re-run fans out in the schedule OWNER's sandbox env, so a non-owner
    notification holder must never be able to re-trigger someone else's schedule. ``schedule.prompt``
    is operator-authored (trusted), so — like retry — the confirm is a plain "are you sure?" pause,
    not a security gate.
    """

    template_name = "accounts/_fix_preview.html"
    # Re-run never renders on the Queue — reject a crafted ``?surface=queue`` (which would target a
    # nonexistent ``queue-item-<id>`` region: the launch fires but nothing swaps).
    _allowed_surfaces = ("feed",)

    def _resolve_schedule(self, request, run_id) -> tuple[Run, ScheduledJob]:
        run = self._resolve_run(request, run_id)
        schedule = run.session.scheduled_job
        if schedule is None or schedule.user_id != request.user.id:
            raise Http404
        return run, schedule

    def get(self, request, *args, **kwargs):
        run, schedule = self._resolve_schedule(request, kwargs["run_id"])
        surface = self._surface(request)
        # Pure read — no write, no enqueue. Re-run is independent of envelope status (a secondary
        # control), so a resolvable owned schedule is the whole gate.
        return self.render_to_response({
            "run": run,
            "fixable": [],
            "surface": surface,
            "region_id": f"{surface}-item-{run.id}",
            "trigger_id": f"rerun-btn-{surface}-{run.id}",
            "can_confirm": True,
            "verb": "rerun",
            "dialog_title": _("Re-run schedule?"),
            "confirm_label": _("Re-run"),
            # Re-run fans out to EVERY entry in ``schedule.repos`` — show the real launch scope, not
            # just the originating run's single repo/ref (honesty: the confirm must not under-report).
            "scope_repos": list(schedule.repos),
            "post_url": reverse("feed_item_rerun", kwargs={"run_id": run.id}) + f"?surface={surface}",
        })

    def post(self, request, *args, **kwargs):
        run, schedule = self._resolve_schedule(request, kwargs["run_id"])
        surface = self._surface(request)
        region_id = f"{surface}-item-{run.id}"
        try:
            # Replicate ScheduleRunNowView.post: repos from ``schedule.repos``, envs resolved against
            # the schedule OWNER (parity with the cron dispatcher), submitted as the requester.
            repos = [RepoTarget(repo_id=r["repo_id"], ref=r["ref"]) for r in schedule.repos]
            # Gate the repos actually launched (schedule.repos), not the originating run's repo.
            if not all(can_run(request.user, t.repo_id) for t in repos):
                return self._dialog_notice(request, _("Repository not found or not accessible."))
            repos = resolve_repo_envs(
                user=schedule.user,
                repos=repos,
                explicit_env_id=str(schedule.sandbox_environment_id) if schedule.sandbox_environment_id else None,
            )
            result = submit_batch_runs(
                user=request.user,
                prompt=schedule.prompt,
                repos=repos,
                trigger_type=SessionOrigin.SCHEDULE,
                scheduled_job=schedule,
                agent_model=schedule.agent_model,
                agent_thinking_level=schedule.agent_thinking_level,
            )
        except RepositoryAccessDenied:
            return self._dialog_notice(request, _("Repository not found or not accessible."))
        except Exception:
            logger.exception(
                "schedule_rerun: launch failed for run=%s schedule=%s user=%s", run.pk, schedule.pk, request.user.pk
            )
            return self._dialog_notice(request, _("Could not start the run. Please try again in a moment."))

        if not result.runs:
            logger.warning(
                "schedule_rerun.launch_failed run_id=%s schedule=%s user=%s errors=%s",
                run.pk,
                schedule.pk,
                request.user.pk,
                [failure.error for failure in result.failed],
            )
            return self._dialog_notice(request, _("Could not start the run. Please try again in a moment."))

        logger.info(
            "schedule_rerun.launched run_id=%s schedule=%s batch_id=%s user=%s",
            run.pk,
            schedule.pk,
            result.batch_id,
            request.user.pk,
        )
        resp = render(
            request,
            "accounts/_launch_started.html",
            {"region_id": region_id, "batch_id": result.batch_id, "started_label": _("Re-run started…")},
        )
        resp["HX-Trigger"] = "rerun:started"
        return resp


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

        # The unified Needs-me Queue (Story 4.1) — one personal-scoped list of still-actionable
        # terminal runs, fronted by the single "you have N" count. Presentation-only.
        context.update(self._get_queue_data(user))

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
        # ``select_related("session")`` so ``build_feed_item``'s ``can_rerun`` read of
        # ``run.session.scheduled_job_id`` (Story 5.2) stays a single join, not a per-row N+1.
        runs_by_id = {
            str(pk): run for pk, run in Run.objects.filter(pk__in=run_ids).select_related("session").in_bulk().items()
        }
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

    def _get_queue_data(self, user: User) -> dict:
        """Build the unified Needs-me Queue: the user's still-actionable TERMINAL runs (Story 4.1).

        ONE personal-scoped list over the three actionable signal classes — (a) FAILED runs, (b)
        open-MR runs, and (c) classifier-flagged runs (``FOUND_ISSUES``/``NEEDS_ATTENTION``, with or
        without an MR) — each confirmed by the shared ``still_actionable`` predicate (the same one
        the Feed and the nav badge use), so no surface can show a contradictory LIVE STATE for the
        same item. That is a per-ITEM guarantee, NOT count equality: the nav badge counts unread,
        schedule-only Feed rows while the Queue spans origins and ignores read-state, so the two
        counts legitimately differ (e.g. a still-classifying successful schedule run can briefly
        show a positive badge over a "nothing needs you." seal until its envelope resolves).

        The candidate filter is deliberately the union of those three classes, NOT a bare "all
        terminal runs filtered by ``still_actionable``": that predicate treats a missing envelope as
        "still classifying ⇒ actionable" (a Feed-context default), so an unfiltered set would flood
        the Queue with plain successful non-schedule jobs that carry no envelope and no MR. The gate
        to ``RunStatus.terminal()`` keeps in-flight webhook/job runs (which already carry a
        ``merge_request_iid`` while RUNNING/QUEUED) out of "needs me". No ``.distinct()`` / no
        ``seen``-set: a single-table OR that reaches the ``envelope`` reverse OneToOne cannot fan out.

        Membership/count/seal reconcile the FULL candidate set (no cap) so the "nothing needs you."
        seal is never a false negative and ``queue_count`` equals the rendered rows (NFR1). The
        cold-cache cost of the per-candidate live MR read rides with the deferred resilience
        follow-up (real batching), NOT a membership cap. Read-only / presentation-only.
        """
        candidates = list(
            Run.objects
            .filter(session__user=user)
            .select_related("session")
            .filter(status__in=RunStatus.terminal())
            .filter(
                Q(status=RunStatus.FAILED)
                | Q(merge_request_iid__isnull=False)
                | Q(envelope__status__in=[EnvelopeStatus.FOUND_ISSUES, EnvelopeStatus.NEEDS_ATTENTION])
            )
            # The ``-id`` tiebreak matches ``RunManager`` / ``RunEnvelope.Meta.ordering``
            # (``("-created_at", "-id")``) so same-``created_at`` runs render in a deterministic
            # order and never reorder between HTMX re-renders.
            .order_by("-created_at", "-id")
        )
        # Batch the candidates' envelopes in one query (no per-row ``for_run``); ``None`` for a run
        # with none is passed straight into ``still_actionable`` (its classifying default).
        envelopes = {
            str(env.run_id): env for env in RunEnvelope.objects.filter(run_id__in=[run.id for run in candidates])
        }

        now = timezone.now()
        items: list[dict] = []
        for run in candidates:
            envelope = envelopes.get(str(run.id))
            if still_actionable(run, envelope):
                items.append(self._build_queue_item(run, envelope, now))
        # Story 4.2 — re-sequence by impact class then age (most-stale first), replacing 4.1's
        # newest-first placeholder. A PURE re-sequence over the already-built items: membership and
        # ``queue_count`` are untouched (AC6), and it adds no query / no live read (AC8).
        items = order_queue(items)
        queue_count = len(items)

        return {
            "queue_items": items,
            "queue_count": queue_count,
            "queue_zero": queue_count == 0,
            # The audit meta is only consulted by the zero-state seal; build it (one ``.exists()``)
            # only when the Queue is actually empty.
            "queue_audit": self._queue_audit(user, len(candidates)) if queue_count == 0 else None,
        }

    @staticmethod
    def _build_queue_item(run: Run, envelope: RunEnvelope | None, now) -> dict:
        """Map ONE still-actionable run to a render-ready Queue row.

        Presentation is decided by (envelope, status, origin) because a ``RunEnvelope`` exists ONLY
        for ``SCHEDULE`` + terminal-SUCCESSFUL runs (``sessions/signals.py``, ``tasks.py``):

        (a) envelope present → its ``status`` slug, the ``_FEED_ACCENT_VARS`` accent, and its
            ``offered_action`` (``FOUND_ISSUES`` → FIX, ``NEEDS_ATTENTION`` → REVIEW);
        (b) no envelope + FAILED → a failed run: RETRY only when ``run.is_retryable`` (which is
            False for webhook/CHAT origins) else NONE — never advertise a retry the domain forbids;
        (c) no envelope + SCHEDULE → genuinely still ``classifying`` (envelope pending in the brief
            post-run window), neutral, NONE;
        (d) no envelope + non-SCHEDULE → an open MR awaiting review (``NEEDS_ATTENTION`` / REVIEW),
            NEVER a permanent "classifying…".
        """
        if envelope is not None:
            status_slug = envelope.status
            accent_var = _FEED_ACCENT_VARS.get(envelope.status, "")
            offered_action = envelope.offered_action
            # A FAILED envelope maps unconditionally to RETRY; enforce the same ``is_retryable``
            # guard branch (b) applies so a domain-forbidden retry is never advertised — both FAILED
            # paths share the guarantee. Latent today (FAILED envelopes only attach to retryable
            # SCHEDULE runs) but removes the divergence between the two FAILED branches.
            if offered_action == OfferedAction.RETRY and not run.is_retryable:
                offered_action = OfferedAction.NONE
        elif run.status == RunStatus.FAILED:
            status_slug = EnvelopeStatus.FAILED
            accent_var = _FEED_ACCENT_VARS[EnvelopeStatus.FAILED]
            offered_action = OfferedAction.RETRY if run.is_retryable else OfferedAction.NONE
        elif run.trigger_type == SessionOrigin.SCHEDULE:
            status_slug = "classifying"
            accent_var = ""
            offered_action = OfferedAction.NONE
        else:
            status_slug = EnvelopeStatus.NEEDS_ATTENTION
            accent_var = _FEED_ACCENT_VARS[EnvelopeStatus.NEEDS_ATTENTION]
            offered_action = OfferedAction.REVIEW
        # Story 4.2 — passive-decay age (AC4): ``finished_at`` (when the item became "done and
        # awaiting you") falling back to ``created_at``. This single clock is BOTH the sort key
        # (``order_queue``) and the staleness threshold, so the row's position and its "stale · Nd"
        # chip can never disagree.
        age_at = run.finished_at or run.created_at
        age = now - age_at
        # Story 5.1 — the fix-able ``actionable[]`` subset (items with a ``fix_prompt``). The Queue
        # only builds still-actionable rows, so the shared ``_fixable_actionable`` gate resolves to a
        # plain filter here; the template offers the ``fix it`` preview iff this is non-empty (a
        # FOUND_ISSUES row with no ``fix_prompt`` keeps its navigate-only link instead).
        return {
            "run_id": run.id,
            "repo_id": run.repo_id,
            "title": run.title or run.repo_id,
            "merge_request_iid": run.merge_request_iid,
            "merge_request_web_url": run.merge_request_web_url,
            "thread_id": run.session.thread_id,
            "created_at": run.created_at,
            "status_slug": status_slug,
            "accent_var": accent_var,
            "offered_action": offered_action,
            "fixable": _fixable_actionable(run, envelope),
            # Impact class attached here so ``order_queue`` sorts on it and a deferred class can be
            # emitted later without touching the sort (AC5). v1: always ``PASSIVE_DECAY``.
            "impact_class": impact_class(run, envelope),
            "age_at": age_at,
            "is_stale": age >= QUEUE_DECAY_STALE_AFTER,
            "stale_days": age.days,
        }

    def _queue_audit(self, user: User, checked_count: int) -> dict:
        """The honest zero-state audit meta (Story 4.1, NFR1).

        ``never-ran`` only when the user has NO TERMINAL runs — an in-flight (QUEUED/RUNNING/READY)
        run is not a check that happened, so a user whose only run is still running gets ``never-ran``
        rather than a false ``audited-clean`` seal. Otherwise ``audited-clean`` with the number of
        candidates actually examined this render (may be 0 → the copy omits the count; NEVER "N runs
        all clear", which would mislabel the failed/merged runs the candidate filter never even
        selected).

        ``last_checked`` is a REAL event time — the most-recent terminal run's ``finished_at`` for
        this user (mirrors the Feed zero-state's ``latest_finished`` max-``finished_at`` idiom), NOT
        ``timezone.now()`` (which advances on every reload and over-claims a check that never
        happened). NULL-safe: the ``finished_at__isnull=False`` filter means a terminal run missing
        its finish degrades ``last_checked`` to ``None`` rather than surfacing a null. ``next_sweep``
        reuses the shared helper.
        """
        if not Run.objects.filter(session__user=user, status__in=RunStatus.terminal()).exists():
            return {"variant": "never-ran"}
        last_checked = (
            Run.objects
            .filter(session__user=user, status__in=RunStatus.terminal(), finished_at__isnull=False)
            .order_by("-finished_at")
            .values_list("finished_at", flat=True)
            .first()
        )
        return {
            "variant": "audited-clean",
            "checked_count": checked_count,
            "last_checked": last_checked,
            "next_sweep": self._next_sweep(user),
        }

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
