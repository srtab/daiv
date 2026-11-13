from django.urls import path

from skills.views import (
    SkillDeleteView,
    SkillDetailView,
    SkillFilesView,
    SkillListView,
    SkillUploadView,
    SkillZipDownloadView,
)

app_name = "skills"

# ``daiv/urls.py`` adds the Models and Usage tabs to this namespace: their views live in apps above this one.
urlpatterns = [
    path("", SkillListView.as_view(), name="list"),
    path("upload/", SkillUploadView.as_view(), name="upload"),
    path("<slug:name>/", SkillDetailView.as_view(), name="detail"),
    path("<slug:name>/files/", SkillFilesView.as_view(), name="files"),
    path("<slug:name>/delete/", SkillDeleteView.as_view(), name="delete"),
    path("<slug:name>/download/", SkillZipDownloadView.as_view(), name="download"),
]
