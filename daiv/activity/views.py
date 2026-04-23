from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, SuspiciousOperation, ValidationError
from django.http import Http404, HttpResponse, HttpResponseBase, StreamingHttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import DetailView, FormView

from django_filters.views import FilterView

from accounts.mixins import BreadcrumbMixin
from activity.filters import ActivityFilter
from activity.forms import AgentRunCreateForm
from activity.models import Activity, ActivityStatus, TriggerType
from activity.services import RepoTarget, submit_batch_runs
from schedules.models import ScheduledJob

logger = logging.getLogger("daiv.activity")

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.http import HttpRequest

    from accounts.models import User


class ActivityListView(LoginRequiredMixin, FilterView):
    model = Activity
    filterset_class = ActivityFilter
    template_name = "activity/activity_list.html"
    context_object_name = "activities"
    paginate_by = 25
    # Preserve pre-django-filter UX: an invalid URL param (e.g. ?status=bogus) should
    # silently drop that filter, not blank the whole list.
    strict = False

    def get_queryset(self) -> QuerySet[Activity]:
        return Activity.objects.by_owner(self.request.user).select_related("task_result", "scheduled_job", "user")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context["filter"].form
        cleaned = form.cleaned_data if form.is_valid() else {}
        context["current_status"] = cleaned.get("status") or ""
        context["current_trigger"] = cleaned.get("trigger") or ""
        context["current_repo"] = cleaned.get("repo") or ""
        context["current_schedule"] = cleaned.get("schedule") or ""
        # Date fields are read raw: cleaned_data yields `date` objects, but the
        # HTML `<input type="date">` needs the original ISO string to round-trip.
        context["current_from"] = self.request.GET.get("date_from", "")
        context["current_to"] = self.request.GET.get("date_to", "")
        context["trigger_types"] = TriggerType.choices
        context["statuses"] = ActivityStatus.choices
        # Resolve schedule name for display
        if schedule_id := context["current_schedule"]:
            schedule = ScheduledJob.objects.filter(pk=schedule_id).values_list("name", flat=True).first()
            context["schedule_name"] = schedule or ""

        # Collect IDs of in-flight activities for SSE
        in_flight = [str(a.id) for a in context["activities"] if a.status not in ActivityStatus.terminal()]
        context["in_flight_ids"] = ",".join(in_flight)

        return context


class ActivityDetailView(BreadcrumbMixin, LoginRequiredMixin, DetailView):
    model = Activity
    template_name = "activity/activity_detail.html"
    context_object_name = "activity"

    def get_queryset(self) -> QuerySet[Activity]:
        return Activity.objects.by_owner(self.request.user).select_related("task_result", "scheduled_job", "user")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        activity: Activity = context["activity"]
        context["is_in_flight"] = activity.status not in ActivityStatus.terminal()

        user = self.request.user
        schedule = activity.scheduled_job
        context["is_schedule_owner_or_admin"] = user.is_admin or (schedule is not None and schedule.user_id == user.pk)
        context["is_subscriber"] = bool(
            schedule is not None and schedule.user_id != user.pk and schedule.subscribers.filter(pk=user.pk).exists()
        )
        return context

    def get_breadcrumbs(self):
        return [
            {"label": "Activity", "url": reverse("activity_list")},
            {"label": f"Run {str(self.object.pk)[:8]} — {self.object.repo_id}", "url": None},
        ]


class ActivityDownloadMarkdownView(LoginRequiredMixin, DetailView):
    """Serve the activity result as a downloadable Markdown file."""

    model = Activity

    def get_queryset(self) -> QuerySet[Activity]:
        return super().get_queryset().filter(status=ActivityStatus.SUCCESSFUL).select_related("task_result")

    def get(self, request, *args, **kwargs):
        activity = self.get_object()
        content = self._build_markdown(activity)
        if not content:
            raise Http404
        filename = self._build_filename(activity)
        response = HttpResponse(content, content_type="text/markdown; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    def _build_markdown(self, activity: Activity) -> str:
        response_text = activity.response_text
        if not response_text:
            return ""

        meta_lines = ["---", f"repository: {activity.repo_id}", f"trigger: {activity.get_trigger_type_display()}"]
        if activity.ref:
            meta_lines.append(f"ref: {activity.ref}")
        meta_lines.append(f"created: {activity.created_at.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        if activity.finished_at:
            meta_lines.append(f"finished: {activity.finished_at.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        if activity.issue_iid:
            meta_lines.append(f"issue: '#{activity.issue_iid}'")
        if activity.merge_request_iid:
            meta_lines.append(f"merge_request: '!{activity.merge_request_iid}'")
        if activity.total_tokens:
            meta_lines.append(f"total_tokens: {activity.total_tokens}")
        if activity.cost_usd is not None:
            meta_lines.append(f"cost_usd: '{activity.cost_usd}'")
        meta_lines.append("---")

        return "\n".join(meta_lines) + "\n\n" + response_text

    def _build_filename(self, activity: Activity) -> str:
        repo_slug = slugify(activity.repo_id.replace("/", "-")) or "unknown"
        date_str = activity.created_at.strftime("%Y-%m-%d")
        return f"daiv-{repo_slug}-{date_str}.md"


POLL_INTERVAL = 2.0
MAX_DURATION = 300.0


class ActivityStreamView(View):
    """SSE endpoint that streams status updates for in-flight activities."""

    async def get(self, request: HttpRequest) -> HttpResponseBase:
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

    async def _stream(self, activity_ids: list[uuid.UUID], user: User):
        """Stream current Activity state to the browser.

        Sync from DBTaskResult happens in the worker via django-tasks signals; this view
        only reads already-synced rows and emits SSE events for state changes.
        """
        tracking = set(activity_ids)
        terminal = ActivityStatus.terminal()
        start = time.monotonic()
        last_emitted: dict[uuid.UUID, tuple[str, str | None, str | None]] = {}

        while tracking and (time.monotonic() - start) < MAX_DURATION:
            await asyncio.sleep(POLL_INTERVAL)

            activities = (
                Activity.objects
                .by_owner(user)
                .filter(id__in=tracking)
                .only("id", "status", "started_at", "finished_at")
            )

            async for activity in activities:
                started_iso = activity.started_at.isoformat() if activity.started_at else None
                finished_iso = activity.finished_at.isoformat() if activity.finished_at else None
                current_state = (activity.status, started_iso, finished_iso)

                if last_emitted.get(activity.id) != current_state:
                    last_emitted[activity.id] = current_state
                    data = json.dumps({
                        "id": str(activity.id),
                        "status": activity.status,
                        "started_at": started_iso,
                        "finished_at": finished_iso,
                    })
                    yield f"data: {data}\n\n"

                if activity.status in terminal:
                    tracking.discard(activity.id)

        yield 'data: {"done": true}\n\n'


class AgentRunCreateView(LoginRequiredMixin, BreadcrumbMixin, FormView):
    """Serve the "Start a run" page and submit new UI-initiated agent runs.

    ``GET /runs/new/`` renders a blank form. ``GET /runs/new/?from=<pk>``
    pre-fills the form from a retryable source Activity. ``POST`` enqueues
    ``run_job_task`` and creates a UI_JOB Activity, redirecting to the detail page.
    """

    template_name = "activity/agent_run_form.html"
    form_class = AgentRunCreateForm

    _SOURCE_UNSET = object()

    def _get_source_activity(self) -> Activity | None:
        # Memoize per-request: ``get_initial`` and ``get_context_data`` both call this on retry GETs.
        cached = getattr(self, "_source_cached", self._SOURCE_UNSET)
        if cached is not self._SOURCE_UNSET:
            return cached
        source_id = self.request.GET.get("from")
        if not source_id:
            self._source_cached = None
            return None
        try:
            source = Activity.objects.by_owner(self.request.user).filter(pk=source_id).first()
        except (ValueError, ValidationError) as err:
            # Malformed UUID on ``?from=`` is user error, not server error.
            raise Http404("Invalid activity id.") from err
        if source is None or not source.is_retryable:
            raise Http404("Activity is not retryable.")
        self._source_cached = source
        return source

    def get_initial(self) -> dict:
        initial: dict = {"notify_on": self.request.user.notify_on_jobs}
        source = self._get_source_activity()
        if source is not None:
            initial.update({
                "prompt": source.prompt,
                "repos_json": json.dumps([{"repo_id": source.repo_id, "ref": source.ref}]),
                "use_max": source.use_max,
            })
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["source_activity"] = self._get_source_activity()
        return ctx

    def form_valid(self, form):
        repos = [RepoTarget(repo_id=r["repo_id"], ref=r["ref"]) for r in form.cleaned_data["repos"]]
        try:
            result = submit_batch_runs(
                user=self.request.user,
                prompt=form.cleaned_data["prompt"],
                repos=repos,
                use_max=form.cleaned_data["use_max"],
                notify_on=form.cleaned_data["notify_on"],
                trigger_type=TriggerType.UI_JOB,
            )
        except Http404, PermissionDenied, SuspiciousOperation:
            # Let Django's middleware render these as 4xx responses instead of
            # masking them as a generic "submit failed" 200.
            raise
        except Exception:
            # Either enqueue failed (no job ran) or the Activity row couldn't be written
            # after enqueue (job is running orphaned). Either way we preserve the form
            # contents and let the user retry; operators see the traceback in logs.
            logger.exception(
                "Failed to submit UI run",
                extra={"user_pk": self.request.user.pk, "repos": form.cleaned_data.get("repos")},
            )
            form.add_error(None, _("Failed to submit the run. Please try again in a moment."))
            return self.form_invalid(form)

        if result.failed:
            from django.contrib import messages as _messages

            failed_ids = ", ".join(f.repo_id for f in result.failed)
            _messages.warning(self.request, _("Some repositories failed to submit: %(ids)s") % {"ids": failed_ids})

        if len(result.activities) == 1 and not result.failed:
            return redirect("activity_detail", pk=result.activities[0].pk)
        return redirect(reverse("activity_list") + f"?batch={result.batch_id}")

    def get_breadcrumbs(self):
        return [{"label": "Activity", "url": reverse("activity_list")}, {"label": "Start a run", "url": None}]
