from django.conf import settings
from django.urls import path

from core.views import HealthCheckView
from daiv.api import api

urlpatterns = [
    path(route="api/", view=api.urls),
    path(route="-/alive/", view=HealthCheckView.as_view(), name="health_check"),
]

if settings.DEBUG:  # pragma: no cover
    from django.conf.urls.static import static
    from django.contrib.staticfiles.urls import staticfiles_urlpatterns

    # Serve static and media files from development server
    urlpatterns += staticfiles_urlpatterns()
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
