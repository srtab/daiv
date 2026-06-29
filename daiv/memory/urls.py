from django.urls import path

from memory.views import MemoryConsolidateView, MemoryDetailView, MemoryListView

app_name = "memory"

urlpatterns = [
    path("", MemoryListView.as_view(), name="list"),
    path("<path:repo_id>/consolidate/", MemoryConsolidateView.as_view(), name="consolidate"),
    path("<path:repo_id>/", MemoryDetailView.as_view(), name="detail"),
]
