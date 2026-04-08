from django.urls import path

from schedules.views import (
    ScheduleCreateView,
    ScheduleDeleteView,
    ScheduleListView,
    ScheduleRunDetailView,
    ScheduleRunListView,
    ScheduleUpdateView,
)

urlpatterns = [
    path("", ScheduleListView.as_view(), name="schedule_list"),
    path("create/", ScheduleCreateView.as_view(), name="schedule_create"),
    path("<int:pk>/edit/", ScheduleUpdateView.as_view(), name="schedule_update"),
    path("<int:pk>/delete/", ScheduleDeleteView.as_view(), name="schedule_delete"),
    path("<int:pk>/runs/", ScheduleRunListView.as_view(), name="schedule_run_list"),
    path("<int:schedule_pk>/runs/<int:pk>/", ScheduleRunDetailView.as_view(), name="schedule_run_detail"),
]
