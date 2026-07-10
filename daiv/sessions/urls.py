from django.urls import path

from sessions.views import (
    RunDownloadMarkdownView,
    SessionDetailView,
    SessionListView,
    SessionNewView,
    SessionStreamView,
)

urlpatterns = [
    path("", SessionListView.as_view(), name="session_list"),
    # Specific "new/*" and "stream/" routes precede the slug catch-all so they match first.
    path("new/", SessionNewView.as_view(), name="session_new"),
    path("new/chat/", SessionDetailView.as_view(), name="session_new_chat"),
    path("stream/", SessionStreamView.as_view(), name="session_stream"),
    path("<slug:thread_id>/", SessionDetailView.as_view(), name="session_detail"),
    path(
        "<slug:thread_id>/runs/<uuid:pk>/download/md/",
        RunDownloadMarkdownView.as_view(),
        name="session_run_download_md",
    ),
]
