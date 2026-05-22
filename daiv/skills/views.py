from __future__ import annotations

import json
import logging

from django.http import HttpResponse
from django.shortcuts import render
from django.views import View
from django.views.generic import TemplateView

from accounts.mixins import AdminRequiredMixin
from skills.forms import SkillUploadForm
from skills.models import GlobalSkill
from skills.services import SkillStorage, list_builtins

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


class SkillUploadView(AdminRequiredMixin, View):
    http_method_names = ["get", "post"]

    def get(self, request):
        return render(request, "skills/_upload_modal.html", {"form": SkillUploadForm()})

    def post(self, request):
        form = SkillUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, "skills/_upload_modal.html", {"form": form})

        package = form.cleaned_data["package"]
        force = bool(form.cleaned_data.get("force"))
        storage = SkillStorage()

        # Conflict detection — covered fully in Task 16
        existing_row = GlobalSkill.objects.filter(name=package.name).first()
        existing_dir = (storage.root / package.name).exists()
        if (existing_row or existing_dir) and not force:
            return render(
                request, "skills/_conflict_confirm.html", {"form": form, "package": package, "existing": existing_row}
            )

        storage.replace(package, uploaded_by=request.user)
        return HttpResponse(status=204, headers={"HX-Trigger": json.dumps({"skill-uploaded": {"name": package.name}})})


class SkillDetailView(_StubAdminView):
    pass


class SkillDeleteView(_StubAdminView):
    pass


class SkillZipDownloadView(_StubAdminView):
    pass
