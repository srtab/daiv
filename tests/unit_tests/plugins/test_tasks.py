from unittest.mock import patch

import pytest
from plugins.models import Plugin
from plugins.tasks import update_plugin_task, update_plugins_cron_task


def make_plugin():
    return Plugin.objects.create(
        scope=Plugin.Scope.GLOBAL,
        name="demo",
        repo_url="https://git.example.com/group/repo.git",
        installed_commit="a" * 40,
    )


def test_cron_task_calls_update_all():
    with patch("plugins.tasks.update_all") as update_all:
        update_plugins_cron_task.func()

    update_all.assert_called_once_with()


@pytest.mark.django_db
def test_update_plugin_task_forces_check():
    plugin = make_plugin()

    with patch("plugins.tasks.check_and_update") as check:
        update_plugin_task.func(plugin.pk)

    check.assert_called_once()
    assert check.call_args.args[0] == plugin
    assert check.call_args.kwargs == {"force": True}


@pytest.mark.django_db
def test_update_plugin_task_ignores_missing_plugin():
    with patch("plugins.tasks.check_and_update") as check:
        update_plugin_task.func(999999)

    check.assert_not_called()


@pytest.mark.django_db
def test_update_plugin_task_ignores_plugin_deleted_during_check():
    plugin = make_plugin()

    with patch("plugins.tasks.check_and_update", side_effect=Plugin.NotUpdated("deleted")):
        update_plugin_task.func(plugin.pk)
