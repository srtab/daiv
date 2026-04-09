from django.urls import path

from activity.views import ActivityDetailView, ActivityListView, ActivityStreamView

urlpatterns = [
    path("", ActivityListView.as_view(), name="activity_list"),
    path("stream/", ActivityStreamView.as_view(), name="activity_stream"),
    path("<uuid:pk>/", ActivityDetailView.as_view(), name="activity_detail"),
]
