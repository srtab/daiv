from django.tasks import task

from crontask import cron

from core.utils import locked_task

from .conf import settings
from .models import Plugin
from .services import check_and_update, update_all


@cron(settings.UPDATE_CRON)
@task
@locked_task(key="plugins-update")
def update_plugins_cron_task():
    update_all()


@task(dedup=True, dedup_on=("plugin_id",), dedup_successful=False)
def update_plugin_task(plugin_id: int):
    try:
        check_and_update(Plugin.objects.get(pk=plugin_id), force=True)
    except Plugin.DoesNotExist, Plugin.NotUpdated:
        return
