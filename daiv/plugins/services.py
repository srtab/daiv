import logging
import os
from itertools import groupby
from operator import attrgetter
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.utils import timezone

from asgiref.sync import async_to_sync

from accounts.utils import aget_platform_identity
from codebase.clients.base import RepoClient
from codebase.conf import settings as codebase_settings

from .fetch import (
    SHA_RE,
    FetchError,
    RepoLocation,
    materialize,
    parse_repo_url,
    prune_revisions,
    resolve_head,
    revision_path,
)
from .manifest import ManifestError, PluginContents, RepoInspection, inspect_repo, read_plugin
from .models import Plugin
from .runtime import PluginAgentRef, PluginSkillRef, ResolvedPlugin

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("daiv.plugins")

OWNED_FIELDS = (
    "last_checked_at",
    "access_lost_at",
    "installed_commit",
    "contents",
    "version",
    "description",
    "last_error",
    "failed_commit",
)
NO_ACCESS = "DAIV can't read this repository."


class InstallError(Exception):
    """A failure to install a plugin, with a message safe to show to the user."""


def _is_public(location: RepoLocation) -> bool:
    return RepoClient.create_instance().repository_visibility(location.slug) == "public"


def _user_can_read(user_id: int, location: RepoLocation) -> bool:
    identity = async_to_sync(aget_platform_identity)(user_id=user_id, provider=codebase_settings.CLIENT)
    return RepoClient.create_instance().user_can_read(
        location.slug, uid=identity.uid if identity else None, username=identity.username if identity else None
    )


def _require_read_access(user, location: RepoLocation) -> None:
    if not location.on_platform:
        return
    try:
        allowed = _is_public(location) or _user_can_read(user.id, location)
    except Exception:
        logger.warning("Could not verify read access to %s", location.slug, exc_info=True)
        raise InstallError(NO_ACCESS) from None
    if not allowed:
        raise InstallError(NO_ACCESS)


def _parse(raw_url: str) -> RepoLocation:
    try:
        return parse_repo_url(raw_url)
    except FetchError as exc:
        raise InstallError(str(exc)) from exc


def _revision(location: RepoLocation, commit: str = "") -> tuple[str, Path]:
    """The files of ``commit`` (default: the remote HEAD), cloned only when they aren't on disk yet; a clone always
    takes the remote HEAD, so the commit returned can differ from the one asked for."""
    try:
        commit = commit or resolve_head(location)
        root = revision_path(location.url, commit)
        if root.is_dir():
            os.utime(root)
            return commit, root
        return materialize(location)
    except FetchError as exc:
        raise InstallError(str(exc)) from exc


def inspect_url(raw_url: str, *, user, commit: str = "") -> tuple[RepoLocation, str, RepoInspection]:
    """Read the plugins in the repository at ``raw_url``: at ``commit`` when it is a commit already on disk (the one
    the user picked from), else at the remote HEAD."""
    location = _parse(raw_url)
    if not user.is_admin:
        _require_read_access(user, location)
    commit, root = _revision(location, commit if SHA_RE.match(commit) else "")
    try:
        return location, commit, inspect_repo(root, repo_slug=location.slug)
    except ManifestError as exc:
        raise InstallError(str(exc)) from exc


def install(*, raw_url: str, commit: str, plugin_paths: list[str], scope: Plugin.Scope, user) -> list[Plugin]:
    is_global = scope == Plugin.Scope.GLOBAL
    if is_global and not user.is_admin:
        raise InstallError("Only administrators can install global plugins.")
    if not SHA_RE.match(commit):
        raise InstallError("The commit is not valid.")

    location = _parse(raw_url)
    if not is_global:
        _require_read_access(user, location)

    commit, root = _revision(location, commit)

    try:
        candidates = {c.plugin_path: c for c in inspect_repo(root, repo_slug=location.slug).candidates if c.supported}
        contents: list[tuple[str, PluginContents]] = []
        for plugin_path in plugin_paths:
            candidate = candidates.get(plugin_path)
            if candidate is None:
                raise InstallError(f"'{plugin_path}' is not an installable plugin in this repository.")
            contents.append((plugin_path, read_plugin(root, plugin_path, fallback_name=candidate.name)))
    except ManifestError as exc:
        raise InstallError(str(exc)) from exc

    owner = None if is_global else user
    existing = set(Plugin.objects.filter(scope=scope, user=owner).values_list("name", flat=True))
    names = [item.name for _, item in contents]
    for name in names:
        if name in existing or names.count(name) > 1:
            raise InstallError(f"A plugin named {name} is already installed.")

    now = timezone.now()
    try:
        with transaction.atomic():
            return [
                Plugin.objects.create(
                    scope=scope,
                    user=owner,
                    installed_by=user,
                    name=item.name,
                    repo_url=location.url,
                    plugin_path=plugin_path,
                    description=item.description,
                    version=item.version,
                    installed_commit=commit,
                    contents=item.to_json(),
                    last_checked_at=now,
                )
                for plugin_path, item in contents
            ]
    except IntegrityError:
        raise InstallError(f"A plugin named {', '.join(names)} is already installed.") from None


def _needs_access_check(plugin: Plugin, location: RepoLocation) -> bool:
    return plugin.scope == Plugin.Scope.USER and location.on_platform


def _refresh_access(plugin: Plugin, location: RepoLocation) -> None:
    try:
        allowed = True if _is_public(location) else _user_can_read(plugin.user_id, location)
    except Exception:
        logger.warning("Could not re-check access for plugin %s", plugin.pk, exc_info=True)
        return
    if allowed:
        plugin.access_lost_at = None
    elif plugin.access_lost_at is None:
        plugin.access_lost_at = timezone.now()


def check_access(plugin: Plugin) -> None:
    plugin.last_checked_at = timezone.now()
    try:
        location = parse_repo_url(plugin.repo_url)
    except FetchError:
        plugin.save(update_fields=["last_checked_at"])
        return
    if _needs_access_check(plugin, location):
        _refresh_access(plugin, location)
    plugin.save(update_fields=["last_checked_at", "access_lost_at", "modified"])


def _record_failure(plugin: Plugin, message: str, commit: str = "") -> None:
    plugin.last_error = message
    if commit:
        plugin.failed_commit = commit


def check_and_update(plugin: Plugin, *, force: bool = False, head: str | None = None) -> None:
    plugin.last_checked_at = timezone.now()
    try:
        _update(plugin, force=force, head=head)
    finally:
        plugin.save(update_fields=[*OWNED_FIELDS, "modified"])


def _update(plugin: Plugin, *, force: bool, head: str | None) -> None:
    try:
        location = parse_repo_url(plugin.repo_url)
    except FetchError as exc:
        _record_failure(plugin, str(exc))
        return

    if _needs_access_check(plugin, location):
        _refresh_access(plugin, location)
        if plugin.access_lost_at is not None:
            _record_failure(plugin, NO_ACCESS)
            return

    try:
        head = head or resolve_head(location)
    except FetchError as exc:
        _record_failure(plugin, str(exc))
        return

    installed_dir = revision_path(location.url, plugin.installed_commit)
    if head == plugin.installed_commit and installed_dir.is_dir():
        plugin.last_error = ""
        plugin.failed_commit = ""
        return
    if head == plugin.failed_commit and not force:
        return

    target = revision_path(location.url, head)
    try:
        if target.is_dir():
            os.utime(target)
            commit, root = head, target
        else:
            commit, root = materialize(location)
    except FetchError as exc:
        _record_failure(plugin, str(exc), head)
        return

    try:
        contents = read_plugin(root, plugin.plugin_path, fallback_name=plugin.name)
    except ManifestError as exc:
        _record_failure(plugin, str(exc), commit)
        return

    if installed_dir.is_dir() and commit != plugin.installed_commit:
        os.utime(installed_dir)
    plugin.installed_commit = commit
    plugin.contents = contents.to_json()
    plugin.version = contents.version
    plugin.description = contents.description
    plugin.last_error = ""
    plugin.failed_commit = ""


def update_all() -> None:
    plugins = list(Plugin.objects.order_by("repo_url", "pk"))
    for repo_url, group in groupby(plugins, key=attrgetter("repo_url")):
        group = list(group)
        auto = [plugin for plugin in group if plugin.auto_update]
        try:
            head = _resolve_once(repo_url, auto)
        except Exception:
            logger.exception("Could not resolve the head of %s", repo_url)
            head = None
        for plugin in group:
            try:
                if plugin.auto_update and head is not None:
                    check_and_update(plugin, head=head)
                else:
                    check_access(plugin)
            except Plugin.NotUpdated:
                logger.info("Plugin %s was removed during the update run", plugin.pk)
            except Exception:
                logger.exception("Plugin %s update failed", plugin.pk)

    keep = {revision_path(plugin.repo_url, plugin.installed_commit) for plugin in plugins if plugin.installed_commit}
    prune_revisions(keep)


def _resolve_once(repo_url: str, auto: list[Plugin]) -> str | None:
    if not auto:
        return None
    try:
        return resolve_head(parse_repo_url(repo_url))
    except FetchError as exc:
        for plugin in auto:
            _record_failure(plugin, str(exc))
            plugin.last_checked_at = timezone.now()
            try:
                plugin.save(update_fields=["last_error", "last_checked_at", "modified"])
            except Plugin.NotUpdated:
                logger.warning("Plugin %s vanished during update", plugin.pk)
        return None


def resolve_plugins_for_user(user_id: int | None) -> list[ResolvedPlugin]:
    chosen = {plugin.name: plugin for plugin in _global_for(user_id)}
    if user_id is not None:
        chosen.update({plugin.name: plugin for plugin in Plugin.objects.user_plugins(user_id).loadable()})

    resolved = []
    for name in sorted(chosen):
        item = _resolve(chosen[name])
        if item is not None:
            resolved.append(item)
    return resolved


def _global_for(user_id: int | None):
    queryset = Plugin.objects.global_plugins().loadable()
    if user_id is not None:
        queryset = queryset.exclude(opt_outs__user_id=user_id)
    return queryset


def _resolve(plugin: Plugin) -> ResolvedPlugin | None:
    base = revision_path(plugin.repo_url, plugin.installed_commit)
    host_root = base / plugin.plugin_path if plugin.plugin_path else base
    if not host_root.is_dir():
        logger.warning("Plugin %s is missing its files at %s", plugin.pk, host_root)
        return None
    try:
        contents = PluginContents.from_json(plugin.contents)
    except KeyError, TypeError:
        logger.warning("Plugin %s has unreadable contents", plugin.pk)
        return None

    resolved = ResolvedPlugin(
        name=plugin.name, commit=plugin.installed_commit, host_root=host_root, skills=(), agents=()
    )
    root = resolved.workspace_root
    skills = sorted(
        (
            PluginSkillRef(f"{plugin.name}:{ref.name}", ref.description, f"{root}/{ref.path}", root)
            for ref in (*contents.skills, *contents.commands)
        ),
        key=attrgetter("name"),
    )
    agents = sorted(
        (
            PluginAgentRef(f"{plugin.name}:{ref.name}", ref.description, f"{root}/{ref.path}", ref.model)
            for ref in contents.agents
        ),
        key=attrgetter("name"),
    )
    return ResolvedPlugin(
        name=plugin.name,
        commit=plugin.installed_commit,
        host_root=host_root,
        skills=tuple(skills),
        agents=tuple(agents),
    )
