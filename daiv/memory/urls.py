from django.urls import path

from memory.views import MemoryConsolidateView, MemoryDetailView, MemoryListView

app_name = "memory"

urlpatterns = [
    path("", MemoryListView.as_view(), name="list"),
    # Literal "consolidate/" prefix keeps this action out of the greedy ``<path:repo_id>`` of
    # the detail route, so a repo whose id ends in "consolidate" stays reachable via detail.
    path("consolidate/<path:repo_id>/", MemoryConsolidateView.as_view(), name="consolidate"),
    path("<path:repo_id>/", MemoryDetailView.as_view(), name="detail"),
]
