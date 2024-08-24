from django.conf import settings
from django.urls import include, path
from django.utils import timezone

from daiv.api import api

last_modified_date = timezone.now()

urlpatterns = [
    path(route="accounts/", view=include("accounts.urls")),
    path(route="api/", view=api.urls),
    path(route="", view=include("core.urls")),
]

if settings.DEBUG:  # pragma: no cover
    from django.conf.urls.static import static
    from django.contrib.staticfiles.urls import staticfiles_urlpatterns

    # Serve static and media files from development server
    urlpatterns += staticfiles_urlpatterns()
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
