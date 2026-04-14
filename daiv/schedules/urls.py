from django.urls import path

from schedules.views import (
    ScheduleCreateView,
    ScheduleDeleteView,
    ScheduleListView,
    ScheduleRunNowView,
    ScheduleToggleView,
    ScheduleUpdateView,
)

urlpatterns = [
    path("", ScheduleListView.as_view(), name="schedule_list"),
    path("create/", ScheduleCreateView.as_view(), name="schedule_create"),
    path("<int:pk>/edit/", ScheduleUpdateView.as_view(), name="schedule_update"),
    path("<int:pk>/delete/", ScheduleDeleteView.as_view(), name="schedule_delete"),
    path("<int:pk>/toggle/", ScheduleToggleView.as_view(), name="schedule_toggle"),
    path("<int:pk>/run/", ScheduleRunNowView.as_view(), name="schedule_run_now"),
]
