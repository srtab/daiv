from django.urls import path

from sessions.views import RunDownloadMarkdownView, SessionDetailView, SessionListView, SessionStreamView

urlpatterns = [
    path("", SessionListView.as_view(), name="session_list"),
    # "new/" and "stream/" must be declared before the slug catch-all so they match first.
    path("new/", SessionDetailView.as_view(), name="session_new"),
    path("stream/", SessionStreamView.as_view(), name="session_stream"),
    path("<slug:thread_id>/", SessionDetailView.as_view(), name="session_detail"),
    path(
        "<slug:thread_id>/runs/<uuid:pk>/download/md/",
        RunDownloadMarkdownView.as_view(),
        name="session_run_download_md",
    ),
]
