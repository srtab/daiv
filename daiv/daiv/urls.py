from django.urls import path

from core.views import HealthCheckView
from daiv.api import api

urlpatterns = [
    path(route="api/", view=api.urls),
    path(route="-/alive/", view=HealthCheckView.as_view(), name="health_check"),
]
