from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import Http404
from django.utils import timezone

import pytest
from plugins.models import Plugin, PluginOptOut


def make_plugin(name="demo", scope=Plugin.Scope.GLOBAL, user=None, **kwargs):
    defaults = {"repo_url": "https://example.com/org/repo", "installed_commit": "a" * 40}
    defaults.update(kwargs)
    return Plugin.objects.create(name=name, scope=scope, user=user, **defaults)


@pytest.mark.django_db
class TestPluginConstraints:
    def test_global_name_unique(self):
        make_plugin()
        with pytest.raises(IntegrityError), transaction.atomic():
            make_plugin()

    def test_user_name_unique_per_user(self, member_user, admin_user):
        make_plugin(scope=Plugin.Scope.USER, user=member_user)
        make_plugin(scope=Plugin.Scope.USER, user=admin_user)
        with pytest.raises(IntegrityError), transaction.atomic():
            make_plugin(scope=Plugin.Scope.USER, user=member_user)

    def test_same_name_global_and_user_allowed(self, member_user):
        make_plugin()
        make_plugin(scope=Plugin.Scope.USER, user=member_user)
        assert Plugin.objects.count() == 2

    def test_scope_shape_rejects_global_with_user(self, member_user):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_plugin(scope=Plugin.Scope.GLOBAL, user=member_user)

    def test_scope_shape_rejects_user_without_user(self):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_plugin(scope=Plugin.Scope.USER, user=None)


@pytest.mark.django_db
class TestPluginQuerySet:
    def test_scoped_get_global_visible_to_member(self, member_user):
        plugin = make_plugin()
        assert Plugin.objects.scoped_get(member_user, plugin.pk) == plugin

    def test_scoped_get_member_cannot_view_other_users_plugin(self, member_user, admin_user):
        plugin = make_plugin(scope=Plugin.Scope.USER, user=admin_user)
        with pytest.raises(Http404):
            Plugin.objects.scoped_get(member_user, plugin.pk)

    def test_scoped_get_admin_can_view_user_plugin(self, member_user, admin_user):
        plugin = make_plugin(scope=Plugin.Scope.USER, user=member_user)
        assert Plugin.objects.scoped_get(admin_user, plugin.pk) == plugin

    def test_manageable_get_member_cannot_manage_global(self, member_user):
        plugin = make_plugin()
        with pytest.raises(PermissionDenied):
            Plugin.objects.manageable_get(member_user, plugin.pk)

    def test_manageable_get_admin_manages_global(self, admin_user):
        plugin = make_plugin()
        assert Plugin.objects.manageable_get(admin_user, plugin.pk) == plugin

    def test_manageable_get_user_plugin_owner_or_admin_only(self, member_user, admin_user):
        plugin = make_plugin(scope=Plugin.Scope.USER, user=admin_user)
        with pytest.raises(Http404):
            Plugin.objects.manageable_get(member_user, plugin.pk)
        assert Plugin.objects.manageable_get(admin_user, plugin.pk) == plugin

    def test_global_and_user_plugins(self, member_user, admin_user):
        global_plugin = make_plugin()
        mine = make_plugin(scope=Plugin.Scope.USER, user=member_user)
        make_plugin(scope=Plugin.Scope.USER, user=admin_user)
        assert list(Plugin.objects.global_plugins()) == [global_plugin]
        assert list(Plugin.objects.user_plugins(member_user)) == [mine]

    def test_loadable_excludes_inactive_and_access_lost(self, member_user):
        ok = make_plugin(name="ok")
        make_plugin(name="inactive", is_active=False)
        make_plugin(name="lost", access_lost_at=timezone.now())
        make_plugin(name="nocommit", installed_commit="")
        assert list(Plugin.objects.loadable()) == [ok]


@pytest.mark.django_db
class TestPluginOptOut:
    def test_opt_out_unique_per_user_and_plugin(self, member_user):
        plugin = make_plugin()
        PluginOptOut.objects.create(user=member_user, plugin=plugin)
        with pytest.raises(IntegrityError), transaction.atomic():
            PluginOptOut.objects.create(user=member_user, plugin=plugin)
