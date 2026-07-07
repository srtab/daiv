from django.urls import path

from sessions.views import SessionListView

urlpatterns = [path("", SessionListView.as_view(), name="session_list")]
