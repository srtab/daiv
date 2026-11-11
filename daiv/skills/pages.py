"""The skill page: a skill's header and its row of tabs.

The SKILL.md and Files tabs are this app's views. The Models tab belongs to ``configuration`` and the Usage tab to
``dashboard``, because they read what this app may not import; all four build on ``SkillPageMixin`` and render a
template that extends ``skills/skill_page.html``.
"""

from __future__ import annotations

import logging
import stat
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Any, ClassVar

from django.http import Http404
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from core.mixins import BreadcrumbMixin
from skills.constants import FRONTMATTER_RE, MAX_FILES
from skills.models import GlobalSkill, SkillInvocation
from skills.services import BUILTIN_SKILL_DIRS, SkillStorage, _skill_metadata, list_builtins, site_model_entries

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("daiv.skills")

SKILL_TABS = (
    ("instructions", _("SKILL.md"), "skills:detail"),
    ("files", _("Files"), "skills:files"),
    ("models", _("Models"), "skills:models"),
    ("usage", _("Usage"), "skills:usage"),
)


@dataclass(frozen=True)
class SkillPage:
    """The skill the agent invokes as ``name``: a custom global skill, else the built-in skill it would shadow."""

    name: str
    description: str
    source: SkillInvocation.Source
    root: Path
    skill_md: str
    global_skill: GlobalSkill | None = None

    @property
    def body(self) -> str:
        return FRONTMATTER_RE.sub("", self.skill_md, count=1).lstrip()

    @property
    def declared_model(self) -> str:
        """The model SKILL.md declares under ``metadata.model``, as the agent reads it; empty when it declares none."""
        metadata = _skill_metadata(self.root) or {}
        return str(metadata.get("metadata", {}).get("model") or "").strip()

    @cached_property
    def files(self) -> list[dict[str, object]]:
        """The skill's regular files, relative to its folder; symlinks are not followed."""
        entries: list[dict[str, object]] = []
        for path in sorted(self.root.rglob("*")):
            if len(entries) >= MAX_FILES:
                break
            try:
                st = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode):
                entries.append({"path": str(path.relative_to(self.root)), "size": st.st_size})
        return entries

    @property
    def file_count(self) -> int:
        """How many files the skill has: an upload's recorded count, which spares walking its folder."""
        return self.global_skill.file_count if self.global_skill is not None else len(self.files)

    def model_entry_count(self) -> int:
        """How many of the skill's model entries are set: its own, and those of the subagents it ships."""
        spec, subagents = site_model_entries(self.name)
        return bool(spec) + subagents


def get_skill_page(name: str) -> SkillPage:
    """``name``'s skill page; ``Http404`` when no such skill exists or its SKILL.md can't be read."""
    global_skill = GlobalSkill.objects.select_related("uploaded_by").filter(name=name).first()
    if global_skill is not None:
        root = SkillStorage().root / global_skill.name
        try:
            skill_md = (root / "SKILL.md").read_text(encoding="utf-8")
        except FileNotFoundError as err:
            raise Http404("skill files missing on disk") from err
        except UnicodeDecodeError as err:
            raise Http404("skill files unreadable on disk") from err
        return SkillPage(name, global_skill.description, SkillInvocation.Source.GLOBAL, root, skill_md, global_skill)

    # Looked up, never joined: a path built from the URL's name is a path-injection sink to CodeQL.
    if (root := BUILTIN_SKILL_DIRS.get(name)) is not None:
        try:
            skill_md = (root / "SKILL.md").read_text(encoding="utf-8")
        except FileNotFoundError as err:
            # A deployment defect, not a client error: log it so Sentry sees it behind the 404.
            logger.error("Built-in skill %r is missing SKILL.md at %s", name, root)
            raise Http404("built-in skill files missing on disk") from err
        description = next((entry["description"] for entry in list_builtins() if entry["name"] == name), "")
        return SkillPage(name, description, SkillInvocation.Source.BUILTIN, root, skill_md)

    raise Http404("skill not found")


class SkillPageMixin(BreadcrumbMixin):
    """A skill page's tab view: ``skill_page`` from the URL's ``name``, and the header and tabs in its context."""

    skill_tab: ClassVar[str]
    kwargs: dict[str, Any]

    @cached_property
    def skill_page(self) -> SkillPage:
        return get_skill_page(self.kwargs["name"])

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        page = self.skill_page
        counts = {"files": page.file_count, "models": page.model_entry_count()}
        return {
            **super().get_context_data(**kwargs),
            "skill": page,
            "invocations_total": SkillInvocation.objects.filter(name=page.name, source=page.source).count(),
            "skill_tabs": [
                {
                    "label": label,
                    "url": reverse(url_name, args=[page.name]),
                    "count": counts.get(key),
                    "current": key == self.skill_tab,
                }
                for key, label, url_name in SKILL_TABS
            ],
        }

    def get_breadcrumbs(self):
        return [{"label": _("Skills"), "url": reverse("skills:list")}, {"label": self.skill_page.name, "url": None}]
