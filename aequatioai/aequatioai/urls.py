from django.conf import settings
from django.urls import include, path
from django.utils import timezone
from django.views.decorators.http import last_modified
from django.views.generic import TemplateView

last_modified_date = timezone.now()

urlpatterns = [
    path(route="accounts/", view=include("accounts.urls")),
    path(
        route="robots.txt",
        view=last_modified(lambda req, **kw: last_modified_date)(
            TemplateView.as_view(template_name="robots.txt", content_type="text/plain")
        ),
    ),
]

if settings.DEBUG:  # pragma: no cover
    from django.conf.urls.static import static
    from django.contrib.staticfiles.urls import staticfiles_urlpatterns

    # Serve static and media files from development server
    urlpatterns += staticfiles_urlpatterns()
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
