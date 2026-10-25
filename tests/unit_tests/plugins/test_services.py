import logging
import os
from unittest.mock import MagicMock, patch

import pytest
from plugins.conf import settings
from plugins.fetch import FetchError, RepoLocation, revision_path
from plugins.manifest import ItemRef, ManifestError, PluginCandidate, PluginContents, RepoInspection
from plugins.models import Plugin, PluginOptOut
from plugins.services import InstallError, check_and_update, inspect_url, install, resolve_plugins_for_user, update_all

from accounts.utils import PlatformIdentity

SHA = "a" * 40
SHA2 = "b" * 40
URL = "https://git.example.com/group/repo.git"
OFFSITE = "https://github.com/acme/repo.git"
PLATFORM = RepoLocation(url=URL, host="git.example.com", on_platform=True, slug="group/repo")
PUBLIC = RepoLocation(url=OFFSITE, host="github.com", on_platform=False, slug="acme/repo")


def contents(name="demo", version="1", skills=(), commands=(), agents=()):
    return PluginContents(
        name=name,
        description="desc",
        version=version,
        skills=tuple(skills),
        commands=tuple(commands),
        agents=tuple(agents),
        skipped=(),
    )


def inspection(name="demo", path="."):
    candidate = PluginCandidate(name=name, plugin_path=path, description="", version="", supported=True, reason="")
    return RepoInspection(marketplace_name="m", candidates=(candidate,))


@pytest.fixture
def data_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_PATH", tmp_path)
    return tmp_path


@pytest.fixture
def client():
    client = MagicMock()
    client.repository_visibility.return_value = "private"
    client.user_can_read.return_value = True
    with patch("plugins.services.RepoClient.create_instance", return_value=client):
        yield client


@pytest.fixture
def identity():
    with patch(
        "plugins.services.aget_platform_identity", return_value=PlatformIdentity(uid=7, username="mem")
    ) as mocked:
        yield mocked


@pytest.fixture
def fetch(data_path):
    def materialize(location):
        target = revision_path(location.url, SHA)
        target.mkdir(parents=True, exist_ok=True)
        return SHA, target

    with (
        patch("plugins.services.parse_repo_url", return_value=PLATFORM),
        patch("plugins.services.materialize", side_effect=materialize) as mat,
        patch("plugins.services.resolve_head", return_value=SHA) as head,
        patch("plugins.services.inspect_repo", return_value=inspection()),
        patch("plugins.services.read_plugin", return_value=contents()) as read,
    ):
        yield MagicMock(materialize=mat, resolve_head=head, read_plugin=read)


def make_plugin(user=None, name="demo", **kwargs):
    defaults = {
        "scope": Plugin.Scope.USER if user else Plugin.Scope.GLOBAL,
        "user": user,
        "name": name,
        "repo_url": URL,
        "installed_commit": SHA,
        "contents": contents(name=name).to_json(),
    }
    return Plugin.objects.create(**{**defaults, **kwargs})


@pytest.mark.django_db
def test_inspect_private_platform_repo_denied_for_non_reader(member_user, fetch, client, identity):
    client.user_can_read.return_value = False

    with pytest.raises(InstallError):
        inspect_url(URL, user=member_user)

    fetch.materialize.assert_not_called()
    client.user_can_read.assert_called_once_with("group/repo", uid=7, username="mem")


@pytest.mark.django_db
def test_inspect_public_repo_needs_no_check(member_user, fetch, client, identity):
    client.repository_visibility.return_value = "public"

    location, commit, found = inspect_url(URL, user=member_user)

    assert (location, commit) == (PLATFORM, SHA)
    assert found.candidates
    client.user_can_read.assert_not_called()


@pytest.mark.django_db
def test_inspect_reuses_the_remote_heads_revision_on_disk(admin_user, fetch, client):
    revision_path(URL, SHA).mkdir(parents=True)

    _, commit, _ = inspect_url(URL, user=admin_user)

    assert commit == SHA
    fetch.resolve_head.assert_called_once()
    fetch.materialize.assert_not_called()


@pytest.mark.django_db
def test_inspect_at_a_picked_commit_on_disk_skips_the_remote(admin_user, fetch, client):
    revision_path(URL, SHA2).mkdir(parents=True)

    _, commit, _ = inspect_url(URL, user=admin_user, commit=SHA2)

    assert commit == SHA2
    fetch.resolve_head.assert_not_called()
    fetch.materialize.assert_not_called()


@pytest.mark.django_db
def test_inspect_ignores_a_malformed_commit(admin_user, fetch, client):
    _, commit, _ = inspect_url(URL, user=admin_user, commit="../..")

    assert commit == SHA
    fetch.resolve_head.assert_called_once()


@pytest.mark.django_db
def test_inspect_admin_skips_read_check(admin_user, fetch, client, identity):
    inspect_url(URL, user=admin_user)

    client.user_can_read.assert_not_called()


@pytest.mark.django_db
def test_inspect_unlinked_user_checks_with_no_identity(member_user, fetch, client):
    client.user_can_read.return_value = False
    with patch("plugins.services.aget_platform_identity", return_value=None), pytest.raises(InstallError):
        inspect_url(URL, user=member_user)

    client.user_can_read.assert_called_once_with("group/repo", uid=None, username=None)


@pytest.mark.django_db
def test_member_cannot_install_global(member_user, fetch, client):
    with pytest.raises(InstallError):
        install(raw_url=URL, commit=SHA, plugin_paths=["."], scope=Plugin.Scope.GLOBAL, user=member_user)

    assert not Plugin.objects.exists()


@pytest.mark.django_db
def test_install_personal_private_checks_read_access(member_user, fetch, client, identity):
    client.user_can_read.return_value = False

    with pytest.raises(InstallError):
        install(raw_url=URL, commit=SHA, plugin_paths=["."], scope=Plugin.Scope.USER, user=member_user)

    assert not Plugin.objects.exists()


@pytest.mark.django_db
def test_install_global_skips_read_check(admin_user, fetch, client, identity):
    install(raw_url=URL, commit=SHA, plugin_paths=["."], scope=Plugin.Scope.GLOBAL, user=admin_user)

    client.user_can_read.assert_not_called()


@pytest.mark.django_db
def test_install_rejects_malformed_commit(admin_user, fetch, client):
    with pytest.raises(InstallError):
        install(raw_url=URL, commit="../..", plugin_paths=["."], scope=Plugin.Scope.GLOBAL, user=admin_user)


@pytest.mark.django_db
def test_install_records_contents_commit_and_installer(member_user, fetch, client, identity):
    (plugin,) = install(raw_url=URL, commit=SHA, plugin_paths=["."], scope=Plugin.Scope.USER, user=member_user)

    assert (plugin.user, plugin.installed_by, plugin.installed_commit, plugin.repo_url) == (
        member_user,
        member_user,
        SHA,
        URL,
    )
    assert plugin.contents == contents().to_json()
    assert plugin.version == "1"


@pytest.mark.django_db
def test_install_rematerializes_missing_revision_and_uses_returned_commit(admin_user, fetch, client, data_path):
    fetch.materialize.side_effect = lambda location: (SHA2, revision_path(location.url, SHA2))
    revision_path(URL, SHA2).mkdir(parents=True)

    (plugin,) = install(raw_url=URL, commit=SHA, plugin_paths=["."], scope=Plugin.Scope.GLOBAL, user=admin_user)

    assert plugin.installed_commit == SHA2


@pytest.mark.django_db
def test_install_unknown_path_rejected(admin_user, fetch, client):
    with pytest.raises(InstallError):
        install(raw_url=URL, commit=SHA, plugin_paths=["nope"], scope=Plugin.Scope.GLOBAL, user=admin_user)


@pytest.mark.django_db
def test_install_root_plugin_listed_beside_external_entry(admin_user, fetch, client):
    root = PluginCandidate(name="demo", plugin_path="", description="", version="", supported=True, reason="")
    external = PluginCandidate(name="ext", plugin_path="", description="", version="", supported=False, reason="no")

    with patch("plugins.services.inspect_repo", return_value=RepoInspection("m", (root, external))):
        (plugin,) = install(raw_url=URL, commit=SHA, plugin_paths=[""], scope=Plugin.Scope.GLOBAL, user=admin_user)

    assert plugin.plugin_path == ""
    fetch.read_plugin.assert_called_once_with(revision_path(URL, SHA), "", fallback_name="demo")


@pytest.mark.django_db
def test_install_manifest_error_becomes_install_error(admin_user, fetch, client):
    fetch.read_plugin.side_effect = ManifestError("bad")

    with pytest.raises(InstallError, match="bad"):
        install(raw_url=URL, commit=SHA, plugin_paths=["."], scope=Plugin.Scope.GLOBAL, user=admin_user)


@pytest.mark.django_db
def test_install_duplicate_name_in_scope_rejected(member_user, admin_user, fetch, client, identity):
    make_plugin(member_user)

    with pytest.raises(InstallError, match="A plugin named demo is already installed."):
        install(raw_url=URL, commit=SHA, plugin_paths=["."], scope=Plugin.Scope.USER, user=member_user)

    (other,) = install(raw_url=URL, commit=SHA, plugin_paths=["."], scope=Plugin.Scope.GLOBAL, user=admin_user)
    assert other.scope == Plugin.Scope.GLOBAL


@pytest.mark.django_db
def test_update_noop_when_head_unchanged(fetch, client, data_path):
    plugin = make_plugin()
    revision_path(URL, SHA).mkdir(parents=True)

    check_and_update(plugin)

    fetch.materialize.assert_not_called()
    plugin.refresh_from_db()
    assert plugin.last_checked_at is not None


@pytest.mark.django_db
def test_update_installs_new_commit(fetch, client, data_path):
    plugin = make_plugin(version="1")
    old = revision_path(URL, SHA)
    old.mkdir(parents=True)
    os.utime(old, (1, 1))
    fetch.resolve_head.return_value = SHA2
    fetch.materialize.side_effect = lambda location: (SHA2, revision_path(location.url, SHA2))
    fetch.read_plugin.return_value = contents(version="2")

    check_and_update(plugin)

    plugin.refresh_from_db()
    assert (plugin.installed_commit, plugin.version, plugin.last_error, plugin.failed_commit) == (SHA2, "2", "", "")
    assert old.stat().st_mtime > 1000


@pytest.mark.django_db
def test_update_reuses_existing_revision_for_given_head(fetch, client, data_path):
    plugin = make_plugin()
    revision_path(URL, SHA2).mkdir(parents=True)

    check_and_update(plugin, head=SHA2)

    fetch.resolve_head.assert_not_called()
    fetch.materialize.assert_not_called()
    plugin.refresh_from_db()
    assert plugin.installed_commit == SHA2


@pytest.mark.django_db
def test_update_failure_keeps_last_good_and_records_failed_commit(fetch, client, data_path):
    plugin = make_plugin(version="1")
    fetch.resolve_head.return_value = SHA2
    fetch.materialize.side_effect = lambda location: (SHA2, revision_path(location.url, SHA2))
    fetch.read_plugin.side_effect = ManifestError("broken manifest")

    check_and_update(plugin)

    plugin.refresh_from_db()
    assert (plugin.installed_commit, plugin.version) == (SHA, "1")
    assert (plugin.failed_commit, plugin.last_error) == (SHA2, "broken manifest")


@pytest.mark.django_db
def test_update_fetch_failure_records_failed_head(fetch, client, data_path):
    plugin = make_plugin()
    fetch.resolve_head.return_value = SHA2
    fetch.materialize.side_effect = FetchError("too big")

    check_and_update(plugin)

    plugin.refresh_from_db()
    assert (plugin.installed_commit, plugin.failed_commit, plugin.last_error) == (SHA, SHA2, "too big")


@pytest.mark.django_db
def test_update_skips_known_failed_commit_unless_forced(fetch, client, data_path):
    plugin = make_plugin(failed_commit=SHA2, last_error="old")
    revision_path(URL, SHA).mkdir(parents=True)
    fetch.resolve_head.return_value = SHA2
    fetch.materialize.side_effect = lambda location: (SHA2, revision_path(location.url, SHA2))

    check_and_update(plugin)
    fetch.materialize.assert_not_called()

    check_and_update(plugin, force=True)
    fetch.materialize.assert_called_once()
    plugin.refresh_from_db()
    assert plugin.installed_commit == SHA2


@pytest.mark.django_db
def test_update_plugin_path_removed_upstream_fails(fetch, client, data_path):
    plugin = make_plugin(plugin_path="plugins/demo")
    fetch.resolve_head.return_value = SHA2
    fetch.materialize.side_effect = lambda location: (SHA2, revision_path(location.url, SHA2))
    fetch.read_plugin.side_effect = ManifestError("Plugin directory 'plugins/demo' does not exist")

    check_and_update(plugin)

    plugin.refresh_from_db()
    assert plugin.installed_commit == SHA
    assert "does not exist" in plugin.last_error


@pytest.mark.django_db
def test_update_rematerializes_when_installed_revision_missing(fetch, client, data_path):
    plugin = make_plugin()

    check_and_update(plugin)

    fetch.materialize.assert_called_once()
    assert revision_path(URL, SHA).is_dir()


@pytest.mark.django_db
def test_access_lost_sets_and_regain_clears(member_user, fetch, client, identity, data_path):
    plugin = make_plugin(member_user)
    revision_path(URL, SHA).mkdir(parents=True)
    client.user_can_read.return_value = False

    check_and_update(plugin)
    plugin.refresh_from_db()
    lost_at = plugin.access_lost_at
    assert lost_at is not None
    assert not Plugin.objects.loadable().exists()

    check_and_update(plugin)
    plugin.refresh_from_db()
    assert plugin.access_lost_at == lost_at

    client.user_can_read.return_value = True
    check_and_update(plugin)
    plugin.refresh_from_db()
    assert plugin.access_lost_at is None


@pytest.mark.django_db
def test_platform_error_leaves_access_unchanged(member_user, fetch, client, identity, data_path):
    plugin = make_plugin(member_user)
    revision_path(URL, SHA).mkdir(parents=True)
    client.user_can_read.side_effect = RuntimeError("503")

    check_and_update(plugin)

    plugin.refresh_from_db()
    assert plugin.access_lost_at is None


@pytest.mark.django_db
def test_global_plugin_never_checks_user_access(fetch, client, data_path):
    plugin = make_plugin()
    revision_path(URL, SHA).mkdir(parents=True)

    check_and_update(plugin)

    client.user_can_read.assert_not_called()


@pytest.mark.django_db
def test_update_all_resolves_each_url_once(member_user, admin_user, fetch, client, data_path):
    make_plugin(name="one")
    make_plugin(name="two", plugin_path="two")
    make_plugin(admin_user, name="three")
    revision_path(URL, SHA).mkdir(parents=True)

    with patch("plugins.services.prune_revisions") as prune:
        update_all()

    fetch.resolve_head.assert_called_once()
    prune.assert_called_once_with({revision_path(URL, SHA)})


@pytest.mark.django_db
def test_inactive_plugins_still_update(fetch, client, data_path):
    plugin = make_plugin(is_active=False)
    fetch.resolve_head.return_value = SHA2
    fetch.materialize.side_effect = lambda location: (SHA2, revision_path(location.url, SHA2))

    update_all()

    plugin.refresh_from_db()
    assert plugin.installed_commit == SHA2


@pytest.mark.django_db
def test_update_all_without_auto_update_still_rechecks_access(member_user, fetch, client, identity, data_path):
    plugin = make_plugin(member_user, auto_update=False)
    fetch.resolve_head.return_value = SHA2
    client.user_can_read.return_value = False

    update_all()

    plugin.refresh_from_db()
    assert plugin.access_lost_at is not None
    assert plugin.installed_commit == SHA
    fetch.resolve_head.assert_not_called()


@pytest.mark.django_db
def test_update_all_head_failure_records_error_for_each_plugin(fetch, client, data_path):
    one = make_plugin(name="one")
    fetch.resolve_head.side_effect = FetchError("down")

    update_all()

    one.refresh_from_db()
    assert one.last_error == "down"


@pytest.mark.django_db
def test_update_all_carries_on_after_an_unexpected_head_error(fetch, client, data_path):
    broken = make_plugin(name="broken")
    other = make_plugin(name="other", repo_url=OFFSITE)
    fetch.resolve_head.side_effect = [RuntimeError("boom"), SHA2]
    fetch.materialize.side_effect = lambda location: (SHA2, revision_path(location.url, SHA2))

    with patch("plugins.services.prune_revisions") as prune:
        update_all()

    broken.refresh_from_db()
    other.refresh_from_db()
    assert (broken.installed_commit, broken.last_checked_at is not None) == (SHA, True)
    assert other.installed_commit == SHA2
    prune.assert_called_once()


def installed_tree(plugin, subdir=""):
    root = revision_path(plugin.repo_url, plugin.installed_commit)
    (root / subdir).mkdir(parents=True, exist_ok=True)


@pytest.mark.django_db
def test_resolve_excludes_opted_out_global(member_user, admin_user, data_path):
    kept = make_plugin(name="kept")
    opted = make_plugin(name="opted")
    installed_tree(kept)
    PluginOptOut.objects.create(user=member_user, plugin=opted)

    assert [p.name for p in resolve_plugins_for_user(member_user.id)] == ["kept"]
    assert [p.name for p in resolve_plugins_for_user(admin_user.id)] == ["kept", "opted"]


@pytest.mark.django_db
def test_resolve_personal_wins_over_global_same_name(member_user, data_path):
    make_plugin(name="demo", repo_url=OFFSITE)
    make_plugin(member_user, name="demo")
    installed_tree(Plugin.objects.get(scope="user"))
    installed_tree(Plugin.objects.get(scope="global"))

    (resolved,) = resolve_plugins_for_user(member_user.id)

    assert resolved.host_root == revision_path(URL, SHA)


@pytest.mark.django_db
def test_resolve_none_user_gets_global_only(member_user, data_path):
    make_plugin(name="glob")
    make_plugin(member_user, name="mine")
    installed_tree(Plugin.objects.get(name="glob"))
    installed_tree(Plugin.objects.get(name="mine"))

    assert [p.name for p in resolve_plugins_for_user(None)] == ["glob"]


@pytest.mark.django_db
def test_resolve_excludes_other_users_personal_plugins(member_user, admin_user, data_path):
    mine = make_plugin(member_user, name="mine")
    installed_tree(mine)

    assert resolve_plugins_for_user(admin_user.id) == []


@pytest.mark.django_db
def test_resolve_skips_unloadable_and_missing_files(member_user, data_path):
    lost = make_plugin(member_user, name="lost", access_lost_at="2026-01-01T00:00:00Z")
    inactive = make_plugin(member_user, name="off", is_active=False, plugin_path="off")
    missing = make_plugin(member_user, name="missing", plugin_path="missing")
    for plugin in (lost, inactive):
        installed_tree(plugin, plugin.plugin_path)
    assert missing

    assert resolve_plugins_for_user(member_user.id) == []


@pytest.mark.django_db
def test_resolved_paths_are_workspace_absolute_and_qualified(data_path):
    items = contents(
        skills=[ItemRef("commit", "Commits", "skills/commit/SKILL.md")],
        commands=[ItemRef("build", "Builds", "commands/build.md")],
        agents=[ItemRef("reviewer", "Reviews", "agents/reviewer.md", "sonnet")],
    )
    plugin = make_plugin(name="devkit", contents=items.to_json(), plugin_path="plugins/devkit")
    installed_tree(plugin, "plugins/devkit")

    (resolved,) = resolve_plugins_for_user(None)

    assert resolved.host_root == revision_path(URL, SHA) / "plugins/devkit"
    assert resolved.workspace_root == "/workspace/plugins/devkit"
    assert [(s.name, s.path, s.plugin_root) for s in resolved.skills] == [
        ("devkit:build", "/workspace/plugins/devkit/commands/build.md", "/workspace/plugins/devkit"),
        ("devkit:commit", "/workspace/plugins/devkit/skills/commit/SKILL.md", "/workspace/plugins/devkit"),
    ]
    (agent,) = resolved.agents
    assert (agent.name, agent.path, agent.model) == (
        "devkit:reviewer",
        "/workspace/plugins/devkit/agents/reviewer.md",
        "sonnet",
    )


@pytest.mark.django_db
@pytest.mark.parametrize("force", [False, True])
def test_lost_access_fails_check_and_never_fetches(member_user, fetch, client, identity, data_path, force):
    plugin = make_plugin(member_user, version="1")
    before = Plugin.objects.get(pk=plugin.pk).contents
    revision_path(URL, SHA).mkdir(parents=True)
    fetch.resolve_head.return_value = SHA2
    client.user_can_read.return_value = False

    check_and_update(plugin, force=force)

    plugin.refresh_from_db()
    assert (plugin.installed_commit, plugin.version, plugin.contents) == (SHA, "1", before)
    assert plugin.access_lost_at is not None
    assert plugin.last_error
    assert plugin.failed_commit == ""
    fetch.resolve_head.assert_not_called()
    fetch.materialize.assert_not_called()


@pytest.mark.django_db
def test_unchanged_head_clears_stale_failure(fetch, client, data_path):
    plugin = make_plugin(failed_commit=SHA2, last_error="old")
    revision_path(URL, SHA).mkdir(parents=True)

    check_and_update(plugin)

    plugin.refresh_from_db()
    assert (plugin.failed_commit, plugin.last_error) == ("", "")


@pytest.mark.django_db(transaction=True)
def test_check_does_not_resurrect_deleted_plugin(fetch, client, data_path):
    stale = make_plugin()
    Plugin.objects.filter(pk=stale.pk).delete()

    with pytest.raises(Plugin.NotUpdated):
        check_and_update(stale)

    assert not Plugin.objects.exists()


@pytest.mark.django_db
def test_check_does_not_revert_concurrent_deactivation(fetch, client, data_path):
    stale = make_plugin()
    revision_path(URL, SHA).mkdir(parents=True)
    Plugin.objects.filter(pk=stale.pk).update(is_active=False)

    check_and_update(stale)

    assert Plugin.objects.get(pk=stale.pk).is_active is False


@pytest.mark.django_db(transaction=True)
def test_update_all_carries_on_after_plugin_deleted_mid_run(fetch, client, data_path, caplog):
    gone = make_plugin(name="gone")
    kept = make_plugin(name="kept", plugin_path="kept")
    revision_path(URL, SHA).mkdir(parents=True)

    def resolve(location):
        Plugin.objects.filter(pk=gone.pk).delete()
        return SHA2

    fetch.resolve_head.side_effect = resolve
    fetch.materialize.side_effect = lambda location: (SHA2, revision_path(location.url, SHA2))

    with caplog.at_level(logging.INFO, logger="daiv.plugins"):
        update_all()

    kept.refresh_from_db()
    assert kept.installed_commit == SHA2
    assert not Plugin.objects.filter(pk=gone.pk).exists()
    assert [r.levelno for r in caplog.records if r.args == (gone.pk,)] == [logging.INFO]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
