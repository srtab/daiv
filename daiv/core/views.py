from django.http import HttpResponse, JsonResponse
from django.views import View

from daiv import BUILD_DATE, GIT_SHA, __version__


class HealthCheckView(View):
    """
    Simple health check endpoint that returns 200 OK.
    """

    async def get(self, request, *args, **kwargs):
        return HttpResponse("OK", content_type="text/plain")


class VersionView(View):
    """
    Report the running build: package version plus the git SHA and build date stamped
    into the image at build time (empty when running from source).
    """

    async def get(self, request, *args, **kwargs):
        return JsonResponse({"version": __version__, "sha": GIT_SHA or None, "build_date": BUILD_DATE or None})
