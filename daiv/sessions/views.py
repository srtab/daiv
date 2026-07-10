from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from functools import cached_property
from typing import TYPE_CHECKING, Any

from django.contrib import messages as messages_module
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, SuspiciousOperation, ValidationError
from django.db.models import Prefetch
from django.http import Http404, HttpResponse, HttpResponseBase, StreamingHttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import DetailView, FormView, TemplateView

from asgiref.sync import async_to_sync, sync_to_async
from django_filters.views import FilterView
from sandbox_envs.models import SandboxEnvironment
from sandbox_envs.services import env_picker_context, resolve_repo_envs

from accounts.mixins import BreadcrumbMixin
from automation.agent.picker_context import agent_picker_context
from chat.repo_state import aget_existing_mr_payload
from chat.turns import build_turns
from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE, RepositoryAccessDenied, can_run
from core.utils import is_htmx
from schedules.models import ScheduledJob
from sessions.filters import RANGE_CHOICES, SessionFilter
from sessions.forms import AgentRunCreateForm
from sessions.hydration import ahydrate_thread
from sessions.locks import stale_cutoff
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.services import RepoTarget, submit_batch_runs

logger = logging.getLogger("daiv.sessions")

if TYPE_CHECKING:
    from django.db.models import QuerySet


POLL_INTERVAL = 2.0
MAX_DURATION = 300.0


class SessionStreamView(View):
    """SSE endpoint that streams Run status updates for in-flight sessions."""

    async def get(self, request: HttpResponseBase) -> HttpResponseBase:
        user = await request.auser()
        if not user.is_authenticated:
            return HttpResponse(status=403)

        ids_param = request.GET.get("ids", "")
        uuids: list[uuid.UUID] = []
        for part in ids_param.split(","):
            try:
                uuids.append(uuid.UUID(part.strip()))
            except ValueError:
                continue

        if not uuids:
            return HttpResponse(status=400)

        return StreamingHttpResponse(
            self._stream(uuids, user),
            content_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _stream(self, run_ids: list[uuid.UUID], user):
        """Stream current Run state to the browser.

        Sync from DBTaskResult happens in the worker via django-tasks signals; this view
        only reads already-synced rows and emits SSE events for state changes.
        """
        terminal = RunStatus.terminal()
        # Authorize the requested ids once: run visibility is stable for the life of the
        # stream, so re-running the (distinct-join) visibility filter every tick is wasted work.
        # Restricting ``tracking`` to visible ids also lets the loop finish cleanly instead
        # of spinning to MAX_DURATION on ids the caller can't see. ``visible_to`` resolves the
        # caller's platform identity with a sync DB read, so build the queryset off-loop.
        visible = await sync_to_async(Run.objects.visible_to)(user)
        tracking: set[uuid.UUID] = {rid async for rid in visible.filter(id__in=run_ids).values_list("id", flat=True)}
        start = time.monotonic()
        last_emitted: dict[uuid.UUID, tuple[str, str | None, str | None]] = {}

        while tracking and (time.monotonic() - start) < MAX_DURATION:
            await asyncio.sleep(POLL_INTERVAL)

            runs = Run.objects.filter(id__in=tracking).only("id", "status", "started_at", "finished_at")

            async for run in runs:
                started_iso = run.started_at.isoformat() if run.started_at else None
                finished_iso = run.finished_at.isoformat() if run.finished_at else None
                current_state = (run.status, started_iso, finished_iso)

                if last_emitted.get(run.id) != current_state:
                    last_emitted[run.id] = current_state
                    data = json.dumps({
                        "id": str(run.id),
                        "status": run.status,
                        "started_at": started_iso,
                        "finished_at": finished_iso,
                    })
                    yield f"data: {data}\n\n"

                if run.status in terminal:
                    tracking.discard(run.id)

        # ``complete`` distinguishes a clean finish (all tracked runs reached a
        # terminal state) from a timeout with runs still pending, so the client
        # can decide whether to re-subscribe rather than freeze on stale state.
        done = json.dumps({"done": True, "complete": not tracking})
        yield f"data: {done}\n\n"


class SessionListView(LoginRequiredMixin, FilterView):
    model = Session
    filterset_class = SessionFilter
    template_name = "sessions/session_list.html"
    context_object_name = "sessions"
    paginate_by = 25
    # Preserve UX: an invalid URL param (e.g. ?status=bogus) should
    # silently drop that filter, not blank the whole list.
    strict = False

    def get_template_names(self) -> list[str]:
        # HTMX requests get just the results fragment so the filter bar and page
        # chrome stay put; a normal GET renders the full page (deep-link / no-JS safe).
        if is_htmx(self.request):
            return ["sessions/_session_results.html"]
        return ["sessions/session_list.html"]

    def get_queryset(self) -> QuerySet[Session]:
        # ``latest_run_status`` (annotation) still drives the status FILTER. Row DISPLAY
        # (latest status/duration/MR/cost, run count) reads the prefetched runs, which also
        # gives the SSE dot the real ``Run.id`` to key live updates on.
        return (
            Session.objects
            .visible_to(self.request.user)
            .with_latest_status()
            .select_related("user", "scheduled_job")
            # Only the columns the row reads (status/duration/MR/cost/SSE id) — skips the fat
            # prompt/result_summary/error_message/usage_by_model columns. Add a field here if
            # the row template starts reading it, or it becomes a deferred-field N+1.
            .prefetch_related(
                Prefetch(
                    "runs",
                    queryset=Run.objects.only(
                        "id",
                        "session_id",
                        "status",
                        "started_at",
                        "finished_at",
                        "merge_request_web_url",
                        "cost_usd",
                        "created_at",
                    ).order_by("-created_at", "-id"),
                )
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context["filter"].form
        cleaned = form.cleaned_data if form.is_valid() else {}
        context["current_status"] = cleaned.get("status") or ""
        context["current_trigger"] = cleaned.get("trigger") or ""
        context["current_trigger_label"] = dict(SessionOrigin.choices).get(context["current_trigger"], "")
        context["current_repo"] = cleaned.get("repo") or ""
        context["current_schedule"] = cleaned.get("schedule") or ""
        context["current_batch"] = cleaned.get("batch") or ""
        context["current_batch_short"] = str(context["current_batch"])[:8] if context["current_batch"] else ""
        context["current_q"] = cleaned.get("q") or ""
        context["current_range"] = cleaned.get("range") or ""
        context["current_range_label"] = dict(RANGE_CHOICES).get(context["current_range"], "")
        # Date fields are read raw: cleaned_data yields `date` objects, but the
        # HTML `<input type="date">` needs the original ISO string to round-trip.
        context["current_from"] = self.request.GET.get("date_from", "")
        context["current_to"] = self.request.GET.get("date_to", "")
        context["has_active_filters"] = any([
            context["current_q"],
            context["current_status"],
            context["current_trigger"],
            context["current_repo"],
            context["current_schedule"],
            context["current_batch"],
            context["current_range"],
            context["current_from"],
            context["current_to"],
        ])
        context["origins"] = SessionOrigin.choices
        context["statuses"] = RunStatus.choices
        context["ranges"] = RANGE_CHOICES

        # Resolve schedule name for the filter-bar chip. Only the full page renders the
        # filter bar, so skip this extra query on the HTMX results-fragment path.
        if not is_htmx(self.request) and (schedule_id := context["current_schedule"]):
            schedule = ScheduledJob.objects.filter(pk=schedule_id).values_list("name", flat=True).first()
            context["schedule_name"] = schedule or ""

        # In-flight RUN ids across the page's sessions, for the SSE status stream.
        page_ids = [s.pk for s in context["sessions"]]
        in_flight = Run.objects.filter(session_id__in=page_ids).exclude(status__in=RunStatus.terminal())
        context["in_flight_ids"] = ",".join(str(rid) for rid in in_flight.values_list("id", flat=True))

        return context


class SessionNewView(LoginRequiredMixin, BreadcrumbMixin, TemplateView):
    """Single front door: choose Chat ('work with the agent') or Run ('hand off a task').

    The chat hero and the run form are unchanged; this page only routes to them and
    carries the one-line rule of thumb so the choice is legible at the fork.
    """

    template_name = "sessions/session_new.html"

    def get_breadcrumbs(self):
        return [{"label": "Sessions", "url": reverse("session_list")}, {"label": "New", "url": None}]


class SessionDetailView(LoginRequiredMixin, BreadcrumbMixin, DetailView):
    """Renders the session transcript page, or the empty state for the ``session_new_chat`` route."""

    model = Session
    template_name = "sessions/session_detail.html"
    context_object_name = "session"
    pk_url_kwarg = "thread_id"

    def get_queryset(self) -> QuerySet[Session]:
        return Session.objects.visible_to(self.request.user).select_related(
            "user", "sandbox_environment", "scheduled_job"
        )

    def get_object(self, queryset=None):
        if "thread_id" not in self.kwargs:
            return None  # empty state (session_new_chat route)
        return super().get_object(queryset)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        session = ctx.setdefault("session", None)

        # Populate sandbox envs both for the empty hero state and a live session.
        ctx["sandbox_envs"] = list(SandboxEnvironment.objects.visible_to(self.request.user))
        ctx["selected_sandbox_env_id"] = (
            str(session.sandbox_environment_id) if session is not None and session.sandbox_environment_id else ""
        )
        ctx["selected_sandbox_env"] = next(
            (e for e in ctx["sandbox_envs"] if str(e.id) == ctx["selected_sandbox_env_id"]), None
        )

        ctx.update(
            agent_picker_context(
                initial_model=session.agent_model if session is not None else "",
                initial_thinking_level=session.agent_thinking_level if session is not None else "",
            )
        )

        if session is None:
            ctx.update({
                "turns": [],
                "expired": False,
                "active_run_id": "",
                "merge_request": None,
                "runs": [],
                "is_in_flight": False,
                "in_flight_ids": "",
                "failed_run": None,
            })
            return ctx

        messages_history, expired, merge_request = async_to_sync(ahydrate_thread)(session.thread_id)
        if merge_request is None and session.repo_id and session.ref:
            merge_request = async_to_sync(aget_existing_mr_payload)(session.repo_id, session.ref)

        runs = list(session.runs.order_by("created_at"))
        non_terminal = [r for r in runs if r.status not in RunStatus.terminal()]
        # A live holder — a chat stream or ``run_job_task`` — bumps the session
        # heartbeat (``last_active_at``) every ~60s, so the session is only really in
        # flight while that heartbeat is fresh. A holder stranded past
        # ``STALE_RUN_MINUTES`` (crashed worker, orphaned queue entry) is dead: falling
        # through to "not in flight" surfaces the expired banner instead of pinning
        # the view on a permanent "working" state. This reuses the staleness signal
        # ``SessionLock`` / ``sync_stuck_runs`` use to decide a holder is dead.
        is_in_flight = bool(non_terminal) and session.last_active_at >= stale_cutoff()

        # A freshly submitted run has not checkpointed yet, so "no checkpoint" only means
        # the session is really over once nothing is (freshly) in flight; while in flight the
        # "working" state and transcript poller render the same view a chat session gets.
        no_state = expired and not is_in_flight
        # ``ahydrate_thread`` reports "no checkpoint" as ``expired`` for two very different
        # reasons (see ``HydratedThread``): a checkpoint that lapsed its TTL, and a thread
        # that never checkpointed. A run that FAILED before it could checkpoint (e.g. a git
        # clone error seconds in) is the second case — the run failed, the state did not
        # expire. Surface that run (and its error) instead of a misleading TTL banner.
        latest_run = runs[-1] if runs else None
        failed_run = latest_run if no_state and latest_run and latest_run.status == RunStatus.FAILED else None

        ctx["turns"] = build_turns(messages_history)
        # A run that failed before checkpointing leaves no transcript, but its prompt
        # survives on the Run. Replay it as a user turn so the page shows what was asked
        # rather than an empty view. ``errored`` is a boolean flag only — it drives the
        # icon + red border on the turn. The raw traceback is developer-only and stays in
        # the logs; it is deliberately not put on the turn (the payload is serialised into
        # the page via ``json_script``, so a raw error here would leak into the HTML).
        if failed_run is not None and failed_run.prompt:
            ctx["turns"].append({
                "id": f"run-{failed_run.id}",
                "role": "user",
                "segments": [{"type": "text", "content": failed_run.prompt}],
                "errored": True,
            })
        ctx["failed_run"] = failed_run
        ctx["expired"] = no_state and failed_run is None
        ctx["active_run_id"] = session.active_run_id or ""
        ctx["merge_request"] = merge_request
        ctx["runs"] = runs
        ctx["is_in_flight"] = is_in_flight
        ctx["in_flight_ids"] = ",".join(str(r.id) for r in non_terminal) if is_in_flight else ""

        # Engage transcript polling when a background run holds the slot and there is
        # no live chat stream from this tab (chat stream manages its own turns in JS;
        # the poller only kicks in for non-chat background runs).
        ctx["poll_transcript"] = bool(
            is_in_flight
            and self.object.active_run_id
            and any(r.trigger_type != SessionOrigin.CHAT for r in non_terminal)
        )

        return ctx

    def get_breadcrumbs(self):
        sessions_url = reverse("session_list")
        session = getattr(self, "object", None)
        if session is None:
            return [{"label": "Sessions", "url": sessions_url}, {"label": "New", "url": None}]
        return [
            {"label": "Sessions", "url": sessions_url},
            {"label": session.title or session.thread_id[:8], "url": None},
        ]


class RunDownloadMarkdownView(LoginRequiredMixin, DetailView):
    """Serve a run's result as a downloadable Markdown file."""

    model = Run

    def get_queryset(self) -> QuerySet[Run]:
        return (
            Run.objects
            .visible_to(self.request.user)
            .filter(status=RunStatus.SUCCESSFUL, session_id=self.kwargs["thread_id"])
            .select_related("session")
        )

    def get(self, request, *args, **kwargs):
        run = self.get_object()
        content = self._build_markdown(run)
        if not content:
            raise Http404
        filename = self._build_filename(run)
        response = HttpResponse(content, content_type="text/markdown; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    def _build_markdown(self, run: Run) -> str:
        response_text = run.response_text
        if not response_text:
            return ""

        meta_lines = ["---", f"repository: {run.repo_id}", f"trigger: {run.get_trigger_type_display()}"]
        if run.ref:
            meta_lines.append(f"ref: {run.ref}")
        meta_lines.append(f"created: {run.created_at.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        if run.finished_at:
            meta_lines.append(f"finished: {run.finished_at.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        if run.merge_request_iid:
            meta_lines.append(f"merge_request: '!{run.merge_request_iid}'")
        if run.total_tokens:
            meta_lines.append(f"total_tokens: {run.total_tokens}")
        if run.cost_usd is not None:
            meta_lines.append(f"cost_usd: '{run.cost_usd}'")
        meta_lines.append("---")

        return "\n".join(meta_lines) + "\n\n" + response_text

    def _build_filename(self, run: Run) -> str:
        repo_slug = slugify(run.repo_id.replace("/", "-")) or "unknown"
        date_str = run.created_at.strftime("%Y-%m-%d")
        return f"daiv-{repo_slug}-{date_str}.md"


class AgentRunCreateView(LoginRequiredMixin, BreadcrumbMixin, FormView):
    """Serve the "Start a run" page and submit new UI-initiated agent runs.

    ``GET /runs/new/`` renders a blank form. ``GET /runs/new/?from=<pk>``
    pre-fills the form from a retryable source Run. ``POST`` enqueues
    ``run_job_task`` and creates a Run, redirecting to the session detail page.
    """

    template_name = "sessions/agent_run_form.html"
    form_class = AgentRunCreateForm

    @cached_property
    def source_run(self) -> Run | None:
        # Cached per-request: ``get_initial`` and ``get_context_data`` both read this on retry GETs.
        source_id = self.request.GET.get("from")
        if not source_id:
            return None
        try:
            source = Run.objects.visible_to(self.request.user).filter(pk=source_id).first()
        except (ValueError, ValidationError) as err:
            raise Http404("Invalid run id.") from err
        # A visible run on a repo the caller can no longer run on must not prefill a
        # retry form — submission would be rejected downstream anyway. Fall back to blank.
        if source is not None and not can_run(self.request.user, source.repo_id):
            return None
        return source

    def get_initial(self) -> dict:
        initial: dict = {"notify_on": self.request.user.notify_on_jobs}
        source = self.source_run
        if source is not None:
            initial.update({
                "prompt": source.prompt,
                "repos": [{"repo_id": source.repo_id, "ref": source.ref}],
                "agent_model": source.agent_model,
                "agent_thinking_level": source.agent_thinking_level,
            })
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["source_run"] = self.source_run
        ctx.update(env_picker_context(ctx["form"]))
        ctx.update(agent_picker_context(ctx["form"]))
        return ctx

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        repos = [RepoTarget(repo_id=r["repo_id"], ref=r["ref"]) for r in form.cleaned_data["repos"]]
        env = form.cleaned_data.get("sandbox_environment")
        repos = resolve_repo_envs(user=self.request.user, repos=repos, explicit_env_id=str(env.id) if env else None)
        try:
            result = submit_batch_runs(
                user=self.request.user,
                prompt=form.cleaned_data["prompt"],
                repos=repos,
                agent_model=form.cleaned_data["agent_model"],
                agent_thinking_level=form.cleaned_data["agent_thinking_level"],
                notify_on=form.cleaned_data["notify_on"],
                trigger_type=SessionOrigin.UI_JOB,
            )
        except Http404, PermissionDenied, SuspiciousOperation:
            raise
        except RepositoryAccessDenied:
            # Access can be revoked between form.clean() and submit; surface it on the field
            # rather than as a generic failure.
            form.add_error("repos", REPO_ACCESS_DENIED_MESSAGE)
            return self.form_invalid(form)
        except Exception:
            logger.exception(
                "Failed to submit UI run",
                extra={"user_pk": self.request.user.pk, "repos": form.cleaned_data.get("repos")},
            )
            form.add_error(None, _("Failed to submit the run. Please try again in a moment."))
            return self.form_invalid(form)

        if result.failed:
            failed_ids = ", ".join(f.repo_id for f in result.failed)
            messages_module.warning(
                self.request, _("Some repositories failed to submit: %(ids)s") % {"ids": failed_ids}
            )

        # Always land on the batch-scoped sessions list — the "hand off" model:
        # fire the run, see it queued/running in the list, walk away.
        return redirect(reverse("session_list") + f"?batch={result.batch_id}")

    def get_breadcrumbs(self):
        return [{"label": "Sessions", "url": reverse("session_list")}, {"label": "Start a run", "url": None}]
