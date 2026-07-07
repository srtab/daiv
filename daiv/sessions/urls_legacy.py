from django.urls import path
from django.views.generic import RedirectView

from sessions.redirect_views import LegacyActivityDetailRedirectView

legacy_activity_urlpatterns = [
    path("", RedirectView.as_view(pattern_name="session_list", permanent=True)),
    path("<uuid:pk>/", LegacyActivityDetailRedirectView.as_view()),
]

legacy_chat_urlpatterns = [
    path("", RedirectView.as_view(pattern_name="session_list", permanent=True)),
    path("new/", RedirectView.as_view(pattern_name="session_new", permanent=True)),
    path("<slug:thread_id>/", RedirectView.as_view(pattern_name="session_detail", permanent=True)),
]
