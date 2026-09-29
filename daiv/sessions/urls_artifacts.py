from django.urls import path

from sessions.views import ArtifactListView

urlpatterns = [path("", ArtifactListView.as_view(), name="artifact_list")]
