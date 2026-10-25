from __future__ import annotations

from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

import pytest
from plugins.manifest import PluginContents
from plugins.models import Plugin, PluginOptOut

SHA = "bb92a77c0ffee0123456789abcdef0123456789a"


def make_plugin(*, name="superpowers", scope=Plugin.Scope.USER, user=None, skipped=(), **kwargs) -> Plugin:
    contents = PluginContents.from_json({
        "name": name,
        "description": "",
        "version": "7.0.0",
        "skills": [{"name": "brainstorming", "description": "d", "path": "skills/b/SKILL.md", "model": None}],
        "commands": [],
        "agents": [],
        "skipped": [{"component": c, "detail": d} for c, d in skipped],
    }).to_json()
    defaults = {
        "scope": scope,
        "user": user if scope == Plugin.Scope.USER else None,
        "name": name,
        "repo_url": "https://github.com/obra/superpowers",
        "version": "7.0.0",
        "installed_commit": SHA,
        "contents": contents,
        "last_checked_at": timezone.now(),
    }
    return Plugin.objects.create(**{**defaults, **kwargs})


def test_list_requires_login(client):
    assert client.get(reverse("plugins:list")).status_code == 302


def test_list_shows_own_and_global_not_others(client, member_user, admin_user):
    make_plugin(name="mine", user=member_user)
    make_plugin(name="theirs", user=admin_user)
    make_plugin(name="shared", scope=Plugin.Scope.GLOBAL)
    client.force_login(member_user)

    html = client.get(reverse("plugins:list")).content.decode()

    assert "mine" in html
    assert "shared" in html
    assert "theirs" not in html


def test_member_cannot_open_global_list(client, member_user):
    client.force_login(member_user)

    assert client.get(reverse("plugins:global_list")).status_code == 403


def test_global_list_shows_turned_off_by_count(client, admin_user, member_user):
    plugin = make_plugin(name="shared", scope=Plugin.Scope.GLOBAL)
    PluginOptOut.objects.create(user=member_user, plugin=plugin)
    client.force_login(admin_user)

    resp = client.get(reverse("plugins:global_list"))

    assert resp.status_code == 200
    assert "1 user" in resp.content.decode()


def test_detail_other_users_plugin_404(client, member_user, admin_user):
    plugin = make_plugin(user=admin_user)
    client.force_login(member_user)

    assert client.get(reverse("plugins:detail", args=[plugin.pk])).status_code == 404


def test_detail_shows_skipped_and_failed_banner(client, member_user):
    plugin = make_plugin(
        user=member_user, skipped=[("hooks", "hooks/hooks.json")], failed_commit="a91f3d2" + "0" * 33, last_error="boom"
    )
    client.force_login(member_user)

    html = client.get(reverse("plugins:detail", args=[plugin.pk])).content.decode()

    assert "superpowers:brainstorming" in html
    assert "hooks/hooks.json" in html
    assert "Not used by DAIV" in html
    assert "a91f3d2" in html
    assert "boom" in html
    assert "Runs keep using" in html
    assert "bb92a77" in html


def test_detail_access_lost_banner_says_runs_no_longer_load_it(client, member_user):
    plugin = make_plugin(user=member_user, access_lost_at=timezone.now())
    client.force_login(member_user)

    html = client.get(reverse("plugins:detail", args=[plugin.pk])).content.decode()

    assert "You can no longer read this repository on the platform." in html
    assert "no longer load" in html


@pytest.mark.parametrize(
    ("kwargs", "label"),
    [
        ({"is_active": False}, "Inactive"),
        ({"failed_commit": "a" * 40}, "Update failed"),
        ({"access_lost_at": timezone.now()}, "Update failed"),
        ({}, "Up to date"),
    ],
)
def test_list_status_labels(client, member_user, kwargs, label):
    make_plugin(user=member_user, **kwargs)
    client.force_login(member_user)

    assert label in client.get(reverse("plugins:list")).content.decode()


def test_list_global_status_off_for_everyone_and_off_for_you(client, member_user):
    make_plugin(name="off-all", scope=Plugin.Scope.GLOBAL, is_active=False)
    opted = make_plugin(name="off-me", scope=Plugin.Scope.GLOBAL)
    PluginOptOut.objects.create(user=member_user, plugin=opted)
    client.force_login(member_user)

    html = client.get(reverse("plugins:list")).content.decode()

    assert "Off for everyone" in html
    assert "Off for you" in html


def test_member_cannot_toggle_global(client, member_user):
    plugin = make_plugin(scope=Plugin.Scope.GLOBAL)
    client.force_login(member_user)

    resp = client.post(reverse("plugins:set_active", args=[plugin.pk]), {"active": "off"})

    assert resp.status_code == 403
    plugin.refresh_from_db()
    assert plugin.is_active


def test_owner_toggles_active(client, member_user):
    plugin = make_plugin(user=member_user)
    client.force_login(member_user)

    resp = client.post(reverse("plugins:set_active", args=[plugin.pk]), {"active": "off"})

    assert resp.status_code == 302
    plugin.refresh_from_db()
    assert not plugin.is_active
    client.post(reverse("plugins:set_active", args=[plugin.pk]), {"active": "on"})
    plugin.refresh_from_db()
    assert plugin.is_active


def test_owner_toggles_auto_update(client, member_user):
    plugin = make_plugin(user=member_user)
    client.force_login(member_user)

    client.post(reverse("plugins:set_auto_update", args=[plugin.pk]), {"auto_update": "off"})

    plugin.refresh_from_db()
    assert not plugin.auto_update


@pytest.mark.parametrize("name", ["set_active", "set_auto_update"])
def test_toggle_rejects_bad_value(client, member_user, name):
    plugin = make_plugin(user=member_user)
    client.force_login(member_user)

    resp = client.post(reverse(f"plugins:{name}", args=[plugin.pk]), {"active": "maybe", "auto_update": "maybe"})

    assert resp.status_code == 400


def test_opt_out_round_trip(client, member_user):
    plugin = make_plugin(scope=Plugin.Scope.GLOBAL)
    client.force_login(member_user)
    url = reverse("plugins:set_opt_out", args=[plugin.pk])

    client.post(url, {"use": "off"})
    assert PluginOptOut.objects.filter(user=member_user, plugin=plugin).exists()

    client.post(url, {"use": "off"})
    assert PluginOptOut.objects.filter(user=member_user, plugin=plugin).count() == 1

    client.post(url, {"use": "on"})
    assert not PluginOptOut.objects.filter(user=member_user, plugin=plugin).exists()


def test_opt_out_rejected_for_personal_plugin(client, member_user):
    plugin = make_plugin(user=member_user)
    client.force_login(member_user)

    resp = client.post(reverse("plugins:set_opt_out", args=[plugin.pk]), {"use": "off"})

    assert resp.status_code == 400
    assert not PluginOptOut.objects.exists()


def test_user_switch_disabled_when_admin_turned_global_off(client, member_user):
    make_plugin(scope=Plugin.Scope.GLOBAL, is_active=False)
    client.force_login(member_user)

    html = client.get(reverse("plugins:list")).content.decode()

    assert "disabled" in html


def test_update_now_enqueues_task(client, member_user):
    plugin = make_plugin(user=member_user)
    client.force_login(member_user)

    with patch("plugins.views.update_plugin_task") as task:
        resp = client.post(reverse("plugins:update_now", args=[plugin.pk]))

    assert resp.status_code == 302
    task.enqueue.assert_called_once_with(plugin_id=plugin.pk)


def test_update_now_forbidden_for_member_on_global(client, member_user):
    plugin = make_plugin(scope=Plugin.Scope.GLOBAL)
    client.force_login(member_user)

    with patch("plugins.views.update_plugin_task") as task:
        resp = client.post(reverse("plugins:update_now", args=[plugin.pk]))

    assert resp.status_code == 403
    task.enqueue.assert_not_called()


def test_delete_confirm_then_delete(client, member_user):
    plugin = make_plugin(user=member_user)
    client.force_login(member_user)
    url = reverse("plugins:delete", args=[plugin.pk])

    assert client.get(url).status_code == 200
    assert Plugin.objects.filter(pk=plugin.pk).exists()

    resp = client.post(url)

    assert resp.status_code == 302
    assert not Plugin.objects.filter(pk=plugin.pk).exists()


def test_admin_deletes_global_plugin_and_lands_on_global_list(client, admin_user):
    plugin = make_plugin(scope=Plugin.Scope.GLOBAL)
    client.force_login(admin_user)

    resp = client.post(reverse("plugins:delete", args=[plugin.pk]))

    assert resp.url == reverse("plugins:global_list")


def test_admin_detail_of_global_plugin_highlights_global_section(client, admin_user):
    plugin = make_plugin(scope=Plugin.Scope.GLOBAL)
    client.force_login(admin_user)

    resp = client.get(reverse("plugins:detail", args=[plugin.pk]))

    assert resp.context["nav_active_section"] == "plugins_global"


def test_member_detail_of_global_plugin_highlights_plugins_section(client, member_user):
    plugin = make_plugin(scope=Plugin.Scope.GLOBAL)
    client.force_login(member_user)

    resp = client.get(reverse("plugins:detail", args=[plugin.pk]))

    assert resp.context["nav_active_section"] == "plugins"


def _inspection(*candidates):
    from plugins.manifest import RepoInspection

    return RepoInspection(marketplace_name="market", candidates=tuple(candidates))


def _cand(name="devkit", path="plugins/devkit", supported=True, reason=""):
    from plugins.manifest import PluginCandidate

    return PluginCandidate(
        name=name, plugin_path=path, description=f"{name} desc", version="0.6.0", supported=supported, reason=reason
    )


def _contents(name="devkit", skills=("a", "b", "c", "d", "e")):
    from plugins.manifest import ItemRef

    return PluginContents(
        name=name,
        description="",
        version="0.6.0",
        skills=tuple(ItemRef(name=s, description="", path=f"skills/{s}/SKILL.md") for s in skills),
        commands=(),
        agents=(),
        skipped=(),
    )


def _location():
    from plugins.fetch import RepoLocation

    return RepoLocation(url="https://github.com/a/b", host="github.com", on_platform=False, slug="a/b")


def test_add_get_shows_url_form_and_hint(client, member_user):
    client.force_login(member_user)

    with patch("plugins.views.RepoClient.create_instance") as create:
        create.return_value.git_host.return_value = "git.example.com"
        html = client.get(reverse("plugins:add")).content.decode()

    assert "Repository link" in html
    assert "private repos only from" in html
    assert "git.example.com" in html


def test_add_get_renders_hint_without_host_when_client_fails(client, member_user):
    client.force_login(member_user)

    with patch("plugins.views.RepoClient.create_instance", side_effect=RuntimeError):
        response = client.get(reverse("plugins:add"))

    assert response.status_code == 200
    assert "Public repos from any host" in response.content.decode()


def test_add_inspect_renders_candidates(client, member_user):
    client.force_login(member_user)
    inspection = _inspection(_cand(), _cand(name="management", path="plugins/management"))

    with (
        patch("plugins.services.inspect_url", return_value=(_location(), SHA, inspection)),
        patch("plugins.views.read_plugin", side_effect=lambda root, path, fallback_name: _contents(fallback_name)),
    ):
        html = client.post(
            reverse("plugins:add"), {"step": "inspect", "url": "https://github.com/a/b"}
        ).content.decode()

    assert "devkit:a" in html
    assert "devkit:c" in html
    assert "devkit:d" not in html
    assert "+2 more" in html
    assert "management" in html
    assert 'value="plugins/devkit"' in html
    assert f'value="{SHA}"' in html
    assert "Everyone" not in html


def test_add_inspect_offers_everyone_to_admin(client, admin_user):
    client.force_login(admin_user)

    with (
        patch("plugins.services.inspect_url", return_value=(_location(), SHA, _inspection(_cand()))),
        patch("plugins.views.read_plugin", return_value=_contents()),
    ):
        html = client.post(
            reverse("plugins:add"), {"step": "inspect", "url": "https://github.com/a/b"}
        ).content.decode()

    assert "Everyone" in html


def test_add_inspect_shows_fetch_error_message(client, member_user):
    from plugins.services import InstallError

    client.force_login(member_user)

    with patch("plugins.services.inspect_url", side_effect=InstallError("DAIV can't read this repository.")):
        response = client.post(reverse("plugins:add"), {"step": "inspect", "url": "https://github.com/a/b"})

    assert response.status_code == 200
    assert "DAIV can&#x27;t read this repository." in response.content.decode()


def test_add_unsupported_candidate_not_selectable(client, member_user):
    from plugins.manifest import ManifestError

    client.force_login(member_user)
    inspection = _inspection(
        _cand(name="hooky", path="plugins/hooky", supported=False, reason="Only hooks"),
        _cand(name="broken", path="plugins/broken"),
        _cand(),
    )

    def read(root, path, fallback_name):
        if fallback_name == "broken":
            raise ManifestError("bad manifest")
        return _contents(fallback_name)

    with (
        patch("plugins.services.inspect_url", return_value=(_location(), SHA, inspection)),
        patch("plugins.views.read_plugin", side_effect=read),
    ):
        html = client.post(
            reverse("plugins:add"), {"step": "inspect", "url": "https://github.com/a/b"}
        ).content.decode()

    assert "Only hooks" in html
    assert "bad manifest" in html
    assert 'value="plugins/hooky"' not in html
    assert 'value="plugins/broken"' not in html
    assert 'value="plugins/devkit"' in html


def _install_post(**overrides):
    return {
        "step": "install",
        "url": "https://github.com/a/b",
        "commit": SHA,
        "plugin_paths": ["plugins/devkit"],
        "scope": "user",
        **overrides,
    }


def test_add_install_creates_plugins_and_redirects(client, member_user):
    client.force_login(member_user)

    with (
        patch("plugins.services.inspect_url", return_value=(_location(), SHA, _inspection(_cand()))),
        patch("plugins.services.install", return_value=[make_plugin(name="devkit", user=member_user)]) as install,
    ):
        response = client.post(reverse("plugins:add"), _install_post())

    assert response.status_code == 302
    assert response.url == reverse("plugins:list")
    install.assert_called_once_with(
        raw_url="https://github.com/a/b",
        commit=SHA,
        plugin_paths=["plugins/devkit"],
        scope=Plugin.Scope.USER,
        user=member_user,
    )


def test_add_install_error_is_shown(client, member_user):
    from plugins.services import InstallError

    client.force_login(member_user)

    with (
        patch("plugins.services.inspect_url", return_value=(_location(), SHA, _inspection(_cand()))),
        patch("plugins.views.read_plugin", return_value=_contents()),
        patch("plugins.services.install", side_effect=InstallError("A plugin named devkit is already installed.")),
    ):
        response = client.post(reverse("plugins:add"), _install_post())

    assert response.status_code == 200
    assert "already installed" in response.content.decode()


def test_member_posting_global_scope_rejected(client, member_user):
    client.force_login(member_user)

    with (
        patch("plugins.services.inspect_url", return_value=(_location(), SHA, _inspection(_cand()))),
        patch("plugins.views.read_plugin", return_value=_contents()),
        patch("plugins.services.install") as install,
    ):
        response = client.post(reverse("plugins:add"), _install_post(scope="global"))

    assert response.status_code == 200
    install.assert_not_called()


def test_add_requires_login(client):
    assert client.get(reverse("plugins:add")).status_code == 302
