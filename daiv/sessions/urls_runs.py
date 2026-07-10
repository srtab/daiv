from django.urls import path

from sessions.views import AgentRunCreateView

app_name = "runs"

urlpatterns = [path("new/", AgentRunCreateView.as_view(), name="agent_run_new")]
