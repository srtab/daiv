from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponsePermanentRedirect
from django.urls import reverse
from django.views import View

from sessions.models import Run


class LegacyActivityDetailRedirectView(LoginRequiredMixin, View):
    """Old /dashboard/activity/<uuid>/ links resolve the Run (same UUID as the old
    Activity) and land on its session, anchored to the run card."""

    def get(self, request, pk):
        run = Run.objects.visible_to(request.user).filter(pk=pk).first()
        if run is None:
            raise Http404
        url = reverse("session_detail", kwargs={"thread_id": run.session_id}) + f"#run-{run.pk}"
        return HttpResponsePermanentRedirect(url)
