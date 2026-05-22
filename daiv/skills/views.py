from __future__ import annotations

import logging

from django.http import HttpResponse
from django.views import View
from django.views.generic import TemplateView

from accounts.mixins import AdminRequiredMixin
from skills.models import GlobalSkill
from skills.services import list_builtins

logger = logging.getLogger("daiv.skills")


class _StubAdminView(AdminRequiredMixin, View):
    """Placeholder until the real view is implemented in a later task."""

    def get(self, request, *args, **kwargs):
        return HttpResponse(status=501)

    def post(self, request, *args, **kwargs):
        return HttpResponse(status=501)


class SkillListView(AdminRequiredMixin, TemplateView):
    template_name = "skills/list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["custom_skills"] = list(GlobalSkill.objects.select_related("uploaded_by"))
        ctx["builtin_skills"] = list_builtins()
        return ctx


class SkillUploadView(_StubAdminView):
    pass


class SkillDetailView(_StubAdminView):
    pass


class SkillDeleteView(_StubAdminView):
    pass


class SkillZipDownloadView(_StubAdminView):
    pass
