from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from django.views import View
from django.views.generic import TemplateView

from codebase.clients import RepoClient
from core.mixins import AdminRequiredMixin
from plugins import services
from plugins.fetch import revision_dir
from plugins.forms import InstallForm, RepoURLForm
from plugins.manifest import ManifestError, PluginContents, read_plugin
from plugins.models import Plugin, PluginOptOut
from plugins.tasks import update_plugin_task

ACCESS_LOST_REASON = _("You can no longer read this repository on the platform.")


def _contents(plugin: Plugin) -> PluginContents | None:
    try:
        return PluginContents.from_json(plugin.contents)
    except KeyError, TypeError:
        return None


def _summary(contents: PluginContents | None) -> str:
    if contents is None:
        return ""
    parts = [
        ngettext("%(n)d skill", "%(n)d skills", len(contents.skills)) % {"n": len(contents.skills)}
        if contents.skills
        else "",
        ngettext("%(n)d command", "%(n)d commands", len(contents.commands)) % {"n": len(contents.commands)}
        if contents.commands
        else "",
        ngettext("%(n)d agent", "%(n)d agents", len(contents.agents)) % {"n": len(contents.agents)}
        if contents.agents
        else "",
    ]
    return " · ".join(p for p in parts if p)


def _source(plugin: Plugin) -> str:
    source = plugin.repo_url.removeprefix("https://").removeprefix("http://").removesuffix(".git").rstrip("/")
    return f"{source} · {plugin.plugin_path}" if plugin.plugin_path else source


def _status(plugin: Plugin, *, opted_out: bool) -> tuple[str, str, str]:
    """Return ``(label, badge class, detail)`` for the first matching state in precedence order."""
    is_global = plugin.scope == Plugin.Scope.GLOBAL
    if not plugin.is_active:
        if is_global:
            return _("Off for everyone"), "status-badge-pending", _("turned off by an admin")
        return _("Inactive"), "status-badge-pending", _("still updating")
    if opted_out:
        return _("Off for you"), "status-badge-pending", _("still on for others")
    if plugin.access_lost_at:
        return _("Update failed"), "status-badge-failed", ACCESS_LOST_REASON
    if plugin.failed_commit or plugin.last_error:
        return _("Update failed"), "status-badge-failed", plugin.last_error
    return _("Up to date"), "status-badge-success", ""


def _decorate(plugins, *, opted_out_ids=frozenset()):
    for plugin in plugins:
        plugin.source_display = _source(plugin)
        plugin.short_commit = plugin.installed_commit[:7]
        plugin.contents_summary = _summary(_contents(plugin))
        plugin.status_label, plugin.status_class, plugin.status_detail = _status(
            plugin, opted_out=plugin.pk in opted_out_ids
        )
        plugin.opted_out = plugin.pk in opted_out_ids
        plugin.use_checked = plugin.is_active and not plugin.opted_out
        plugin.use_locked = not plugin.is_active
        plugin.active_url = reverse("plugins:set_active", args=[plugin.pk])
        plugin.auto_update_url = reverse("plugins:set_auto_update", args=[plugin.pk])
        plugin.use_url = reverse("plugins:set_opt_out", args=[plugin.pk])
    return plugins


def _pin_nav_section(request, plugin: Plugin) -> None:
    if plugin.scope == Plugin.Scope.GLOBAL and request.user.is_admin:
        request.nav_view_name = "plugins:global_list"


def _list_url_for(plugin: Plugin) -> str:
    return reverse("plugins:global_list" if plugin.scope == Plugin.Scope.GLOBAL else "plugins:list")


def _after_action(request, plugin: Plugin) -> str:
    if request.POST.get("next") == "detail":
        return reverse("plugins:detail", args=[plugin.pk])
    if request.POST.get("next") == "list":
        return reverse("plugins:list")
    return _list_url_for(plugin)


class PluginListView(LoginRequiredMixin, TemplateView):
    template_name = "plugins/list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        opted_out = set(PluginOptOut.objects.filter(user=user).values_list("plugin_id", flat=True))
        ctx["your_plugins"] = _decorate(list(Plugin.objects.user_plugins(user)))
        ctx["global_plugins"] = _decorate(list(Plugin.objects.global_plugins()), opted_out_ids=opted_out)
        return ctx


class PluginGlobalListView(AdminRequiredMixin, TemplateView):
    template_name = "plugins/global_list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        rows = Plugin.objects.global_plugins().annotate(opt_out_count=Count("opt_outs"))
        ctx["global_plugins"] = _decorate(list(rows))
        return ctx


class PluginDetailView(LoginRequiredMixin, View):
    http_method_names = ["get"]

    def get(self, request, pk):
        plugin = Plugin.objects.scoped_get(request.user, pk)
        _pin_nav_section(request, plugin)
        _decorate(
            [plugin],
            opted_out_ids=set(
                PluginOptOut.objects.filter(user=request.user, plugin=plugin).values_list("plugin_id", flat=True)
            ),
        )
        contents = _contents(plugin)
        groups = []
        if contents:
            groups = [
                (_("Skills"), contents.skills),
                (_("Commands"), contents.commands),
                (_("Agents"), contents.agents),
            ]
        can_manage = plugin.scope == Plugin.Scope.USER and (plugin.user_id == request.user.id or request.user.is_admin)
        can_manage = can_manage or (plugin.scope == Plugin.Scope.GLOBAL and request.user.is_admin)
        return render(
            request,
            "plugins/detail.html",
            {
                "plugin": plugin,
                "groups": groups,
                "skipped": contents.skipped if contents else (),
                "can_manage": can_manage,
                "is_global": plugin.scope == Plugin.Scope.GLOBAL,
                "check_error": plugin.last_error.rstrip("."),
            },
        )


class _ToggleView(LoginRequiredMixin, View):
    http_method_names = ["post"]
    field: str
    param: str

    def post(self, request, pk):
        plugin = Plugin.objects.manageable_get(request.user, pk)
        value = request.POST.get(self.param)
        if value not in {"on", "off"}:
            return HttpResponseBadRequest("invalid value")
        setattr(plugin, self.field, value == "on")
        plugin.save(update_fields=[self.field, "modified"])
        return redirect(_after_action(request, plugin))


class PluginSetActiveView(_ToggleView):
    field = "is_active"
    param = "active"


class PluginSetAutoUpdateView(_ToggleView):
    field = "auto_update"
    param = "auto_update"


class PluginUpdateNowView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk):
        plugin = Plugin.objects.manageable_get(request.user, pk)
        update_plugin_task.enqueue(plugin_id=plugin.pk)
        messages.success(request, _("Update of '%(name)s' queued.") % {"name": plugin.name})
        return redirect(_after_action(request, plugin))


class PluginSetOptOutView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk):
        plugin = Plugin.objects.scoped_get(request.user, pk)
        value = request.POST.get("use")
        if plugin.scope != Plugin.Scope.GLOBAL or value not in {"on", "off"}:
            return HttpResponseBadRequest("invalid request")
        if value == "off":
            PluginOptOut.objects.get_or_create(user=request.user, plugin=plugin)
        else:
            PluginOptOut.objects.filter(user=request.user, plugin=plugin).delete()
        return redirect(
            reverse("plugins:detail", args=[plugin.pk])
            if request.POST.get("next") == "detail"
            else reverse("plugins:list")
        )


class PluginDeleteView(LoginRequiredMixin, View):
    http_method_names = ["get", "post"]

    def get(self, request, pk):
        plugin = Plugin.objects.manageable_get(request.user, pk)
        _pin_nav_section(request, plugin)
        return render(request, "plugins/confirm_delete.html", {"object": plugin})

    def post(self, request, pk):
        plugin = Plugin.objects.manageable_get(request.user, pk)
        name, url = plugin.name, _list_url_for(plugin)
        plugin.delete()
        messages.success(request, _("Plugin '%(name)s' removed.") % {"name": name})
        return redirect(url)


MAX_PICKER_SKILLS = 3


def _git_host() -> str:
    try:
        return RepoClient.create_instance().git_host()
    except Exception:
        return ""


def _picker_rows(location, commit, candidates) -> list[dict]:
    rows = []
    for candidate in candidates:
        row = {
            "candidate": candidate,
            "selectable": candidate.supported,
            "reason": candidate.reason,
            "skills": [],
            "more": 0,
        }
        if candidate.supported:
            try:
                contents = read_plugin(
                    revision_dir(location, commit), candidate.plugin_path, fallback_name=candidate.name
                )
            except ManifestError as exc:
                row["selectable"], row["reason"] = False, str(exc)
            else:
                names = [f"{contents.name}:{item.name}" for item in contents.skills]
                row["skills"], row["more"] = names[:MAX_PICKER_SKILLS], max(len(names) - MAX_PICKER_SKILLS, 0)
        rows.append(row)
    return rows


class PluginAddView(LoginRequiredMixin, View):
    http_method_names = ["get", "post"]

    def get(self, request):
        return self._render_url_step(request, RepoURLForm())

    def post(self, request):
        if request.POST.get("step") == "install":
            return self._install(request)
        form = RepoURLForm(request.POST)
        if not form.is_valid():
            return self._render_url_step(request, form)
        return self._inspect(request, form.cleaned_data["url"])

    def _render_url_step(self, request, form, *, error=""):
        return render(request, "plugins/add.html", {"url_form": form, "git_host": _git_host(), "error": error})

    def _inspect(self, request, url):
        try:
            location, commit, inspection = services.inspect_url(url, user=request.user)
        except services.InstallError as exc:
            return self._render_url_step(request, RepoURLForm({"url": url}), error=str(exc))
        form = InstallForm(
            initial={"url": url, "commit": commit, "scope": Plugin.Scope.USER},
            candidates=inspection.candidates,
            user=request.user,
        )
        return self._render_picker(request, form, location, commit, inspection, url)

    def _render_picker(self, request, form, location, commit, inspection, url, *, error=""):
        rows = _picker_rows(location, commit, inspection.candidates)
        selected = set(request.POST.getlist("plugin_paths")) if form.is_bound else set()
        for row in rows:
            row["checked"] = row["candidate"].plugin_path in selected
        return render(
            request,
            "plugins/add.html",
            {
                "url_form": RepoURLForm({"url": url}),
                "git_host": _git_host(),
                "form": form,
                "rows": rows,
                "marketplace_name": inspection.marketplace_name,
                "error": error,
            },
        )

    def _install(self, request):
        url = request.POST.get("url", "")
        try:
            location, commit, inspection = services.inspect_url(url, user=request.user)
        except services.InstallError as exc:
            return self._render_url_step(request, RepoURLForm({"url": url}), error=str(exc))
        form = InstallForm(request.POST, candidates=inspection.candidates, user=request.user)
        if not form.is_valid():
            return self._render_picker(request, form, location, commit, inspection, url)
        data = form.cleaned_data
        try:
            plugins = services.install(
                raw_url=data["url"],
                commit=data["commit"],
                plugin_paths=data["plugin_paths"],
                scope=Plugin.Scope(data["scope"]),
                user=request.user,
            )
        except services.InstallError as exc:
            return self._render_picker(request, form, location, commit, inspection, url, error=str(exc))
        messages.success(
            request, ngettext("Installed %(n)d plugin.", "Installed %(n)d plugins.", len(plugins)) % {"n": len(plugins)}
        )
        return redirect(reverse("plugins:list"))
