from django.urls import path

from codebase import views

app_name = "codebase"

urlpatterns = [
    path("pickers/repositories/", views.picker_repositories_view, name="picker-repositories"),
    path("pickers/repositories/<path:slug>/branches/", views.picker_branches_view, name="picker-branches"),
]
