from django.urls import path

from memory.views import MemoryListView

app_name = "memory"

urlpatterns = [path("", MemoryListView.as_view(), name="list")]
