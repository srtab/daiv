from django.urls import path

from schedules.views import (
    ScheduleCreateView,
    ScheduleDeleteView,
    ScheduleDuplicateView,
    ScheduleListView,
    ScheduleRunNowView,
    ScheduleTemplateCreateView,
    ScheduleTemplateDeleteView,
    ScheduleTemplateListView,
    ScheduleTemplateUpdateView,
    ScheduleToggleView,
    ScheduleUnsubscribeView,
    ScheduleUpdateView,
)

urlpatterns = [
    path("", ScheduleListView.as_view(), name="schedule_list"),
    path("create/", ScheduleCreateView.as_view(), name="schedule_create"),
    path("templates/", ScheduleTemplateListView.as_view(), name="schedule_template_list"),
    path("templates/create/", ScheduleTemplateCreateView.as_view(), name="schedule_template_create"),
    path("templates/<int:pk>/edit/", ScheduleTemplateUpdateView.as_view(), name="schedule_template_update"),
    path("templates/<int:pk>/delete/", ScheduleTemplateDeleteView.as_view(), name="schedule_template_delete"),
    path("<int:pk>/edit/", ScheduleUpdateView.as_view(), name="schedule_update"),
    path("<int:pk>/delete/", ScheduleDeleteView.as_view(), name="schedule_delete"),
    path("<int:pk>/toggle/", ScheduleToggleView.as_view(), name="schedule_toggle"),
    path("<int:pk>/run/", ScheduleRunNowView.as_view(), name="schedule_run_now"),
    path("<int:pk>/unsubscribe/", ScheduleUnsubscribeView.as_view(), name="schedule_unsubscribe"),
    path("<int:pk>/duplicate/", ScheduleDuplicateView.as_view(), name="schedule_duplicate"),
]
