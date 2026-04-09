from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from datetime import date
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, StreamingHttpResponse
from django.views import View
from django.views.generic import DetailView, ListView

from activity.models import Activity, ActivityStatus, TriggerType
from schedules.models import ScheduledJob

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.http import HttpRequest


class ActivityListView(LoginRequiredMixin, ListView):
    model = Activity
    template_name = "activity/activity_list.html"
    context_object_name = "activities"
    paginate_by = 25

    def get_queryset(self) -> QuerySet[Activity]:
        qs = super().get_queryset().select_related("task_result", "scheduled_job")

        if (status := self.request.GET.get("status", "")) and status in ActivityStatus.values:
            qs = qs.filter(status=status)

        if (trigger := self.request.GET.get("trigger", "")) and trigger in dict(TriggerType.choices):
            qs = qs.filter(trigger_type=trigger)

        if repo := self.request.GET.get("repo", ""):
            qs = qs.filter(repo_id=repo)

        if schedule_id := self.request.GET.get("schedule", ""):
            with contextlib.suppress(ValueError, TypeError):
                qs = qs.filter(scheduled_job_id=int(schedule_id))

        if date_from := self.request.GET.get("from", ""):
            try:
                date.fromisoformat(date_from)
            except ValueError:
                pass
            else:
                qs = qs.filter(created_at__date__gte=date_from)

        if date_to := self.request.GET.get("to", ""):
            try:
                date.fromisoformat(date_to)
            except ValueError:
                pass
            else:
                qs = qs.filter(created_at__date__lte=date_to)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["current_status"] = self.request.GET.get("status", "")
        context["current_trigger"] = self.request.GET.get("trigger", "")
        context["current_repo"] = self.request.GET.get("repo", "")
        context["current_schedule"] = self.request.GET.get("schedule", "")
        context["current_from"] = self.request.GET.get("from", "")
        context["current_to"] = self.request.GET.get("to", "")
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


class ActivityDetailView(LoginRequiredMixin, DetailView):
    model = Activity
    template_name = "activity/activity_detail.html"
    context_object_name = "activity"

    def get_queryset(self) -> QuerySet[Activity]:
        return super().get_queryset().select_related("task_result", "scheduled_job")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        activity: Activity = context["activity"]
        context["is_in_flight"] = activity.status not in ActivityStatus.terminal()
        return context


POLL_INTERVAL = 2.0
MAX_DURATION = 300.0


class ActivityStreamView(LoginRequiredMixin, View):
    """SSE endpoint that streams status updates for in-flight activities."""

    async def get(self, request: HttpRequest) -> HttpResponse:
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
            self._stream(uuids),
            content_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _stream(self, activity_ids: list[uuid.UUID]):
        tracking = set(activity_ids)
        terminal = ActivityStatus.terminal()
        start = time.monotonic()

        while tracking and (time.monotonic() - start) < MAX_DURATION:
            await asyncio.sleep(POLL_INTERVAL)

            activities = Activity.objects.filter(id__in=tracking).select_related("task_result")

            to_update: list[Activity] = []
            fields_to_update: set[str] = set()
            async for activity in activities:
                changed_fields = activity.sync_from_task_result()
                if changed_fields:
                    to_update.append(activity)
                    fields_to_update.update(changed_fields)
                    data = json.dumps({
                        "id": str(activity.id),
                        "status": activity.status,
                        "started_at": activity.started_at.isoformat() if activity.started_at else None,
                        "finished_at": activity.finished_at.isoformat() if activity.finished_at else None,
                    })
                    yield f"data: {data}\n\n"

                if activity.status in terminal:
                    tracking.discard(activity.id)

            if to_update and fields_to_update:
                await Activity.objects.abulk_update(to_update, fields=fields_to_update)

        yield 'data: {"done": true}\n\n'
