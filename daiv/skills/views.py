from __future__ import annotations

import json
import logging

from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import render
from django.views import View
from django.views.generic import TemplateView

from accounts.mixins import AdminRequiredMixin
from skills.constants import FRONTMATTER_RE, ZIPS_DIR
from skills.forms import SkillUploadForm
from skills.models import GlobalSkill
from skills.services import SkillStorage, list_builtins

logger = logging.getLogger("daiv.skills")


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


class SkillDetailView(AdminRequiredMixin, View):
    http_method_names = ["get"]

    def get(self, request, name):
        skill = self._get_or_404(name)
        root = SkillStorage().root / skill.name
        skill_md_text = (root / "SKILL.md").read_text(encoding="utf-8")
        body = FRONTMATTER_RE.sub("", skill_md_text, count=1).lstrip()
        tree = self._list_tree(root)
        return render(request, "skills/detail.html", {"skill": skill, "body": body, "tree": tree})

    def _get_or_404(self, name: str) -> GlobalSkill:
        try:
            return GlobalSkill.objects.get(name=name)
        except GlobalSkill.DoesNotExist as err:
            raise Http404("skill not found") from err

    @staticmethod
    def _list_tree(root) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            entries.append({"path": str(path.relative_to(root)), "size": path.stat().st_size})
        return entries


class SkillDeleteView(AdminRequiredMixin, View):
    http_method_names = ["get", "post"]

    def _get_or_404(self, name: str) -> GlobalSkill:
        try:
            return GlobalSkill.objects.get(name=name)
        except GlobalSkill.DoesNotExist as err:
            raise Http404("skill not found") from err

    def get(self, request, name):
        skill = self._get_or_404(name)
        return render(request, "skills/_delete_confirm.html", {"skill": skill})

    def post(self, request, name):
        skill = self._get_or_404(name)
        SkillStorage().delete(skill.name)
        return HttpResponse(status=204, headers={"HX-Trigger": json.dumps({"skill-deleted": {"name": skill.name}})})


class SkillZipDownloadView(AdminRequiredMixin, View):
    http_method_names = ["get"]

    def get(self, request, name):
        try:
            skill = GlobalSkill.objects.get(name=name)
        except GlobalSkill.DoesNotExist as err:
            raise Http404("skill not found") from err
        path = SkillStorage().root / ZIPS_DIR / f"{skill.name}.zip"
        if not path.is_file():
            raise Http404("zip not found on disk")
        return FileResponse(
            path.open("rb"), as_attachment=True, filename=f"{skill.name}.zip", content_type="application/zip"
        )
