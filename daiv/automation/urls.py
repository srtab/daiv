from django.urls import path

from automation.views import agent_models_view

app_name = "automation"

urlpatterns = [path("agent/models/", agent_models_view, name="agent_models")]
