from django.urls import path

from skills.views import SkillDeleteView, SkillDetailView, SkillListView, SkillUploadView, SkillZipDownloadView

app_name = "skills"

urlpatterns = [
    path("", SkillListView.as_view(), name="list"),
    path("upload/", SkillUploadView.as_view(), name="upload"),
    path("<slug:name>/", SkillDetailView.as_view(), name="detail"),
    path("<slug:name>/delete/", SkillDeleteView.as_view(), name="delete"),
    path("<slug:name>/download/", SkillZipDownloadView.as_view(), name="download"),
]
