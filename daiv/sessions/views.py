from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponse, HttpResponseBase, StreamingHttpResponse
from django.urls import reverse
from django.utils.text import slugify
from django.views import View
from django.views.generic import DetailView

from asgiref.sync import async_to_sync
from django_filters.views import FilterView
from sandbox_envs.models import SandboxEnvironment

from accounts.mixins import BreadcrumbMixin
from automation.agent.picker_context import agent_picker_context
from chat.repo_state import aget_existing_mr_payload
from chat.turns import build_turns
from schedules.models import ScheduledJob
from sessions.filters import SessionFilter
from sessions.hydration import ahydrate_thread
from sessions.models import Run, RunStatus, Session, SessionOrigin

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
        tracking = set(run_ids)
        terminal = RunStatus.terminal()
        start = time.monotonic()
        last_emitted: dict[uuid.UUID, tuple[str, str | None, str | None]] = {}

        while tracking and (time.monotonic() - start) < MAX_DURATION:
            await asyncio.sleep(POLL_INTERVAL)

            runs = Run.objects.by_owner(user).filter(id__in=tracking).only("id", "status", "started_at", "finished_at")

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

        yield 'data: {"done": true}\n\n'


class SessionListView(LoginRequiredMixin, FilterView):
    model = Session
    filterset_class = SessionFilter
    template_name = "sessions/session_list.html"
    context_object_name = "sessions"
    paginate_by = 25
    # Preserve UX: an invalid URL param (e.g. ?status=bogus) should
    # silently drop that filter, not blank the whole list.
    strict = False

    def get_queryset(self) -> QuerySet[Session]:
        from django.db import models as db_models

        from sessions.models import Run

        user = self.request.user
        # Apply owner scoping first (returns a plain QuerySet), then annotate.
        base_qs = Session.objects.by_owner(user)
        latest = Run.objects.filter(session=db_models.OuterRef("pk")).order_by("-created_at", "-id")
        return (
            base_qs
            .annotate(latest_run_status=db_models.Subquery(latest.values("status")[:1]))
            .select_related("user", "scheduled_job")
            .prefetch_related("runs")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context["filter"].form
        cleaned = form.cleaned_data if form.is_valid() else {}
        context["current_status"] = cleaned.get("status") or ""
        context["current_trigger"] = cleaned.get("trigger") or ""
        context["current_repo"] = cleaned.get("repo") or ""
        context["current_schedule"] = cleaned.get("schedule") or ""
        context["current_batch"] = cleaned.get("batch") or ""
        context["current_batch_short"] = str(context["current_batch"])[:8] if context["current_batch"] else ""
        # Date fields are read raw: cleaned_data yields `date` objects, but the
        # HTML `<input type="date">` needs the original ISO string to round-trip.
        context["current_from"] = self.request.GET.get("date_from", "")
        context["current_to"] = self.request.GET.get("date_to", "")
        context["has_active_filters"] = any([
            context["current_status"],
            context["current_trigger"],
            context["current_repo"],
            context["current_schedule"],
            context["current_batch"],
            context["current_from"],
            context["current_to"],
        ])
        context["origins"] = SessionOrigin.choices
        context["statuses"] = RunStatus.choices

        # Resolve schedule name for display.
        if schedule_id := context["current_schedule"]:
            schedule = ScheduledJob.objects.filter(pk=schedule_id).values_list("name", flat=True).first()
            context["schedule_name"] = schedule or ""

        # In-flight RUN ids across the page's sessions, for the SSE status stream (Task 13).
        page_ids = [s.pk for s in context["sessions"]]
        in_flight = Run.objects.filter(session_id__in=page_ids).exclude(status__in=RunStatus.terminal())
        context["in_flight_ids"] = ",".join(str(rid) for rid in in_flight.values_list("id", flat=True))

        return context


class SessionDetailView(LoginRequiredMixin, BreadcrumbMixin, DetailView):
    """Renders the session transcript page, or the empty state for the ``session_new`` route."""

    model = Session
    template_name = "sessions/session_detail.html"
    context_object_name = "session"
    pk_url_kwarg = "thread_id"

    def get_queryset(self) -> QuerySet[Session]:
        return Session.objects.by_owner(self.request.user).select_related(
            "user", "sandbox_environment", "scheduled_job"
        )

    def get_object(self, queryset=None):
        if "thread_id" not in self.kwargs:
            return None  # empty state (session_new route)
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
            })
            return ctx

        messages_history, expired, merge_request = async_to_sync(ahydrate_thread)(session.thread_id)
        if merge_request is None and session.repo_id and session.ref:
            merge_request = async_to_sync(aget_existing_mr_payload)(session.repo_id, session.ref)

        ctx["turns"] = build_turns(messages_history)
        ctx["expired"] = expired
        ctx["active_run_id"] = session.active_run_id or ""
        ctx["merge_request"] = merge_request

        runs = list(session.runs.order_by("created_at"))
        ctx["runs"] = runs
        ctx["is_in_flight"] = any(r.status not in RunStatus.terminal() for r in runs)
        ctx["in_flight_ids"] = ",".join(str(r.id) for r in runs if r.status not in RunStatus.terminal())

        # Engage transcript polling when a background run holds the slot and there is
        # no live chat stream from this tab (chat stream manages its own turns in JS;
        # the poller only kicks in for non-chat background runs).
        ctx["poll_transcript"] = bool(
            self.object
            and self.object.active_run_id
            and any(r.trigger_type != SessionOrigin.CHAT and r.status not in RunStatus.terminal() for r in ctx["runs"])
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
            .by_owner(self.request.user)
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
