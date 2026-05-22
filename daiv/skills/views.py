from __future__ import annotations

import logging

from django.http import HttpResponse
from django.views import View

from accounts.mixins import AdminRequiredMixin

logger = logging.getLogger("daiv.skills")


class _StubAdminView(AdminRequiredMixin, View):
    """Placeholder until the real view is implemented in a later task."""

    def get(self, request, *args, **kwargs):
        return HttpResponse(status=501)

    def post(self, request, *args, **kwargs):
        return HttpResponse(status=501)


class SkillListView(_StubAdminView):
    pass


class SkillUploadView(_StubAdminView):
    pass


class SkillDetailView(_StubAdminView):
    pass


class SkillDeleteView(_StubAdminView):
    pass


class SkillZipDownloadView(_StubAdminView):
    pass
