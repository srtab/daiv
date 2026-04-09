from django.urls import path

from schedules.views import ScheduleCreateView, ScheduleDeleteView, ScheduleListView, ScheduleUpdateView

urlpatterns = [
    path("", ScheduleListView.as_view(), name="schedule_list"),
    path("create/", ScheduleCreateView.as_view(), name="schedule_create"),
    path("<int:pk>/edit/", ScheduleUpdateView.as_view(), name="schedule_update"),
    path("<int:pk>/delete/", ScheduleDeleteView.as_view(), name="schedule_delete"),
]
