from django.urls import path

from memory.views import MemoryDetailView, MemoryListView

app_name = "memory"

urlpatterns = [
    path("", MemoryListView.as_view(), name="list"),
    path("<path:repo_id>/", MemoryDetailView.as_view(), name="detail"),
]
