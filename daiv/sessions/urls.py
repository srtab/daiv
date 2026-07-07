from django.urls import path

from sessions.views import RunDownloadMarkdownView, SessionDetailView, SessionListView

urlpatterns = [
    path("", SessionListView.as_view(), name="session_list"),
    # "new/" must be declared before the slug catch-all so it is matched first.
    path("new/", SessionDetailView.as_view(), name="session_new"),
    path("<slug:thread_id>/", SessionDetailView.as_view(), name="session_detail"),
    path(
        "<slug:thread_id>/runs/<uuid:pk>/download/md/",
        RunDownloadMarkdownView.as_view(),
        name="session_run_download_md",
    ),
]
