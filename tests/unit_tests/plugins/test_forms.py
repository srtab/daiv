from __future__ import annotations

from plugins.forms import InstallForm
from plugins.manifest import PluginCandidate
from plugins.models import Plugin

SHA = "bb92a77c0ffee0123456789abcdef0123456789a"


def _candidate(name="devkit", path="plugins/devkit", supported=True):
    return PluginCandidate(
        name=name, plugin_path=path, description="d", version="1", supported=supported, reason="" if supported else "no"
    )


def _form(user, data=None, candidates=None):
    return InstallForm(
        {
            "url": "https://github.com/a/b",
            "commit": SHA,
            "scope": "user",
            "plugin_paths": ["plugins/devkit"],
            **(data or {}),
        },
        candidates=candidates or [_candidate()],
        user=user,
    )


def test_install_form_scope_choices_member_vs_admin(member_user, admin_user):
    member = _form(member_user)
    admin = _form(admin_user)

    assert [value for value, _ in member.fields["scope"].choices] == [Plugin.Scope.USER]
    assert [value for value, _ in admin.fields["scope"].choices] == [Plugin.Scope.USER, Plugin.Scope.GLOBAL]


def test_install_form_rejects_unknown_plugin_path(member_user):
    form = _form(member_user, {"plugin_paths": ["plugins/other"]})

    assert not form.is_valid()
    assert "plugin_paths" in form.errors


def test_install_form_rejects_unsupported_plugin_path(member_user):
    form = _form(member_user, candidates=[_candidate(supported=False)])

    assert not form.is_valid()


def test_install_form_rejects_malformed_commit(member_user):
    assert not _form(member_user, {"commit": "main"}).is_valid()


def test_install_form_accepts_valid_selection(member_user):
    form = _form(member_user)

    assert form.is_valid()
    assert form.cleaned_data["scope"] == Plugin.Scope.USER
