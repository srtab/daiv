from urllib.parse import urlencode

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponsePermanentRedirect, HttpResponseRedirect
from django.urls import reverse
from django.views import View

from sessions.models import Run, Session


class LegacyActivityDetailRedirectView(LoginRequiredMixin, View):
    """Old /dashboard/activity/<uuid>/ links resolve the Run (same UUID as the old
    Activity) and land on its session, anchored to the run card."""

    def get(self, request, pk):
        run = Run.objects.visible_to(request.user).filter(pk=pk).first()
        if run is None:
            raise Http404
        url = reverse("session_detail", kwargs={"thread_id": run.session_id}) + f"#run-{run.pk}"
        return HttpResponsePermanentRedirect(url)


class SessionMergeRequestRedirectView(LoginRequiredMixin, View):
    """Target of the session link DAIV writes into merge request descriptions.

    Resolving the merge request here rather than at publish time is what lets the description
    carry a stable URL: the IID does not exist yet when the description is rendered, and later
    sessions keep attaching to the same request. Redirects are deliberately temporary — the
    same thread resolves to the session list once its run backfills the IID.
    """

    def get(self, request, thread_id):
        session = Session.objects.visible_to(request.user).filter(pk=thread_id).first()
        if session is None:
            raise Http404

        merge_request_iid = (
            session.merge_request_iid
            or Run.objects
            .filter(session_id=session.pk, merge_request_iid__isnull=False)
            .values_list("merge_request_iid", flat=True)
            .first()
        )
        if merge_request_iid is None:
            return HttpResponseRedirect(reverse("session_detail", kwargs={"thread_id": session.pk}))

        query = urlencode({"repo": session.repo_id, "mr": merge_request_iid})
        return HttpResponseRedirect(f"{reverse('session_list')}?{query}")
