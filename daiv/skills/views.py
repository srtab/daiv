from __future__ import annotations

import logging
import stat
from datetime import timedelta

from django.contrib import messages
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from accounts.mixins import AdminRequiredMixin
from automation.agent.constants import BUILTIN_SKILLS_PATH
from skills.constants import FRONTMATTER_RE, MAX_FILES, ZIPS_DIR
from skills.forms import SkillUploadForm
from skills.models import GlobalSkill, SkillInvocation
from skills.services import BUILTIN_SKILL_NAMES, SkillStorage, SkillStorageError, list_builtins

logger = logging.getLogger("daiv.skills")


class SkillListView(AdminRequiredMixin, TemplateView):
    template_name = "skills/list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        custom_qs = GlobalSkill.objects.select_related("uploaded_by")
        custom_skills = list(custom_qs)
        # list_builtins() returns an lru_cached list of shared dicts; shallow-copy
        # each entry before annotating so we don't mutate cached state.
        builtin_skills = [dict(entry) for entry in list_builtins()]

        counts = {
            (row["name"], row["source"]): row["c"]
            for row in (SkillInvocation.objects.values("name", "source").annotate(c=Count("id")))
        }

        for skill in custom_skills:
            skill.invocations_count = counts.get((skill.name, SkillInvocation.Source.GLOBAL), 0)
        for entry in builtin_skills:
            entry["invocations_count"] = counts.get((entry["name"], SkillInvocation.Source.BUILTIN), 0)

        ctx["custom_skills"] = custom_skills
        ctx["builtin_skills"] = builtin_skills
        return ctx


class SkillUploadView(AdminRequiredMixin, View):
    http_method_names = ["get", "post"]
    template_name = "skills/skill_upload.html"

    @staticmethod
    def _breadcrumbs():
        return [{"label": _("Skills"), "url": reverse("skills:list")}, {"label": _("Upload skill"), "url": None}]

    def _render(self, request, form, *, package=None, existing=None, conflict=False, status=200):
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "package": package,
                "existing": existing,
                "conflict": conflict,
                "breadcrumbs": self._breadcrumbs(),
            },
            status=status,
        )

    def get(self, request):
        return self._render(request, SkillUploadForm())

    def post(self, request):
        form = SkillUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            return self._render(request, form)

        package = form.cleaned_data["package"]
        force = bool(form.cleaned_data.get("force"))
        storage = SkillStorage()

        existing_row = GlobalSkill.objects.filter(name=package.name).first()
        existing_dir = (storage.root / package.name).exists()
        if (existing_row or existing_dir) and not force:
            return self._render(request, form, package=package, existing=existing_row, conflict=True)

        had_conflict = bool(existing_row or existing_dir)
        try:
            storage.replace(package, uploaded_by=request.user)
        except SkillStorageError as err:
            form.add_error("zip", str(err))
            return self._render(request, form, package=package, existing=existing_row, conflict=had_conflict)
        except OSError:
            logger.exception("Could not save skill %r", package.name)
            form.add_error("zip", _("Could not save the skill. Please try again."))
            return self._render(request, form, package=package, existing=existing_row, conflict=had_conflict)

        messages.success(request, _("Skill '%(name)s' uploaded.") % {"name": package.name})
        return redirect("skills:list")


class SkillDetailView(AdminRequiredMixin, View):
    http_method_names = ["get"]

    def get(self, request, name):
        if name in BUILTIN_SKILL_NAMES:
            return self._render_builtin(request, name)
        return self._render_global(request, name)

    def _render_builtin(self, request, name):
        root = BUILTIN_SKILLS_PATH / name
        try:
            skill_md_text = (root / "SKILL.md").read_text(encoding="utf-8")
        except FileNotFoundError as err:
            # Missing built-in SKILL.md is a deployment defect, not a client
            # error — log loudly so Sentry catches it even though we still
            # have to return 404 to the browser.
            logger.error("Built-in skill %r is missing SKILL.md at %s", name, root)
            raise Http404("built-in skill files missing on disk") from err
        body = FRONTMATTER_RE.sub("", skill_md_text, count=1).lstrip()
        description = next((entry["description"] for entry in list_builtins() if entry["name"] == name), "")
        return render(
            request,
            "skills/detail.html",
            {
                "skill": {"name": name, "description": description},
                "source": "builtin",
                "body": body,
                "tree": self._list_tree(root),
                "usage": self._compute_usage(name, SkillInvocation.Source.BUILTIN),
                "breadcrumbs": [{"label": _("Skills"), "url": reverse("skills:list")}, {"label": name, "url": None}],
            },
        )

    def _render_global(self, request, name):
        skill = get_object_or_404(GlobalSkill, name=name)
        root = SkillStorage().root / skill.name
        try:
            skill_md_text = (root / "SKILL.md").read_text(encoding="utf-8")
        except FileNotFoundError as err:
            raise Http404("skill files missing on disk") from err
        except UnicodeDecodeError as err:
            raise Http404("skill files unreadable on disk") from err
        body = FRONTMATTER_RE.sub("", skill_md_text, count=1).lstrip()
        return render(
            request,
            "skills/detail.html",
            {
                "skill": skill,
                "source": "global",
                "body": body,
                "tree": self._list_tree(root),
                "usage": self._compute_usage(skill.name, SkillInvocation.Source.GLOBAL),
                "breadcrumbs": [
                    {"label": _("Skills"), "url": reverse("skills:list")},
                    {"label": skill.name, "url": None},
                ],
            },
        )

    @staticmethod
    def _compute_usage(name: str, source: SkillInvocation.Source) -> dict:
        qs = SkillInvocation.objects.filter(name=name, source=source)
        today = timezone.localdate()
        cutoff = today - timedelta(days=29)

        grouped = dict(
            qs
            .filter(created__date__gte=cutoff)
            .annotate(day=TruncDate("created"))
            .values("day")
            .annotate(c=Count("id"))
            .values_list("day", "c")
        )
        daily_series = [
            {"day": cutoff + timedelta(days=i), "count": grouped.get(cutoff + timedelta(days=i), 0)} for i in range(30)
        ]
        last_30_total = sum(entry["count"] for entry in daily_series)

        return {
            "total": qs.count(),
            "last_30_total": last_30_total,
            "daily_series": daily_series,
            "recent": list(qs.order_by("-created")[:20]),
        }

    @staticmethod
    def _list_tree(root) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        # lstat() so symlinks dropped on disk out-of-band aren't followed; MAX_FILES matches the upload cap.
        for path in sorted(root.rglob("*")):
            if len(entries) >= MAX_FILES:
                break
            try:
                st = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            entries.append({"path": str(path.relative_to(root)), "size": st.st_size})
        return entries


class SkillDeleteView(AdminRequiredMixin, View):
    http_method_names = ["get", "post"]
    template_name = "skills/skill_confirm_delete.html"

    @staticmethod
    def _breadcrumbs(skill):
        return [
            {"label": _("Skills"), "url": reverse("skills:list")},
            {"label": skill.name, "url": reverse("skills:detail", args=[skill.name])},
            {"label": _("Delete"), "url": None},
        ]

    def get(self, request, name):
        skill = get_object_or_404(GlobalSkill, name=name)
        return render(request, self.template_name, {"object": skill, "breadcrumbs": self._breadcrumbs(skill)})

    def post(self, request, name):
        skill = get_object_or_404(GlobalSkill, name=name)
        try:
            SkillStorage().delete(skill.name)
        except SkillStorageError as err:
            logger.exception("Failed to delete skill %r", skill.name)
            return render(
                request,
                self.template_name,
                {"object": skill, "error": str(err), "breadcrumbs": self._breadcrumbs(skill)},
                status=409,
            )
        except OSError:
            logger.exception("Failed to delete skill %r", skill.name)
            return render(
                request,
                self.template_name,
                {"object": skill, "error": _("Could not delete the skill."), "breadcrumbs": self._breadcrumbs(skill)},
                status=500,
            )
        messages.success(request, _("Skill '%(name)s' deleted.") % {"name": skill.name})
        return redirect("skills:list")


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
