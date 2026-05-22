from __future__ import annotations

import json
import logging

from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from accounts.mixins import AdminRequiredMixin
from skills.constants import FRONTMATTER_RE, ZIPS_DIR
from skills.forms import SkillUploadForm
from skills.models import GlobalSkill
from skills.services import SkillStorage, SkillStorageError, list_builtins

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

        existing_row = GlobalSkill.objects.filter(name=package.name).first()
        existing_dir = (storage.root / package.name).exists()
        if (existing_row or existing_dir) and not force:
            return render(
                request, "skills/_conflict_confirm.html", {"form": form, "package": package, "existing": existing_row}
            )

        try:
            storage.replace(package, uploaded_by=request.user)
        except SkillStorageError as err:
            form.add_error("zip", str(err))
            return render(request, "skills/_upload_modal.html", {"form": form})
        except OSError:
            logger.exception("Could not save skill %r", package.name)
            form.add_error("zip", _("Could not save the skill. Please try again."))
            return render(request, "skills/_upload_modal.html", {"form": form})
        return HttpResponse(status=204, headers={"HX-Trigger": json.dumps({"skill-uploaded": {"name": package.name}})})


class SkillDetailView(AdminRequiredMixin, View):
    http_method_names = ["get"]

    def get(self, request, name):
        skill = get_object_or_404(GlobalSkill, name=name)
        root = SkillStorage().root / skill.name
        try:
            skill_md_text = (root / "SKILL.md").read_text(encoding="utf-8")
        except FileNotFoundError as err:
            raise Http404("skill files missing on disk") from err
        except UnicodeDecodeError as err:
            raise Http404("skill files unreadable on disk") from err
        body = FRONTMATTER_RE.sub("", skill_md_text, count=1).lstrip()
        tree = self._list_tree(root)
        return render(request, "skills/detail.html", {"skill": skill, "body": body, "tree": tree})

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

    def get(self, request, name):
        skill = get_object_or_404(GlobalSkill, name=name)
        return render(request, "skills/_delete_confirm.html", {"skill": skill})

    def post(self, request, name):
        skill = get_object_or_404(GlobalSkill, name=name)
        try:
            SkillStorage().delete(skill.name)
        except (SkillStorageError, OSError) as err:
            logger.exception("Failed to delete skill %r", skill.name)
            message = str(err) if isinstance(err, SkillStorageError) else _("Could not delete the skill.")
            return render(request, "skills/_delete_confirm.html", {"skill": skill, "error": message})
        return HttpResponse(status=204, headers={"HX-Trigger": json.dumps({"skill-deleted": {"name": skill.name}})})


class SkillZipDownloadView(AdminRequiredMixin, View):
    http_method_names = ["get"]

    def get(self, request, name):
        skill = get_object_or_404(GlobalSkill, name=name)
        path = SkillStorage().root / ZIPS_DIR / f"{skill.name}.zip"
        if not path.is_file():
            raise Http404("zip not found on disk")
        return FileResponse(
            path.open("rb"), as_attachment=True, filename=f"{skill.name}.zip", content_type="application/zip"
        )
