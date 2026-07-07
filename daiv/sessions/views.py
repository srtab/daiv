from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin

from django_filters.views import FilterView

from schedules.models import ScheduledJob
from sessions.filters import SessionFilter
from sessions.models import Run, RunStatus, Session, SessionOrigin

if TYPE_CHECKING:
    from django.db.models import QuerySet


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
