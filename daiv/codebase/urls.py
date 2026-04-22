from django.urls import path

from codebase import views

app_name = "codebase"

urlpatterns = [path("pickers/repositories/", views.picker_repositories_view, name="picker-repositories")]
