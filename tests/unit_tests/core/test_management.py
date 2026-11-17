import signal
from unittest.mock import Mock

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.management import call_command
from django.core.management.base import CommandError

import pytest
from pydantic import HttpUrl

from core.conf import settings as core_settings


class TestDbWorker:
    def test_it_is_the_db_worker_command_django_admin_runs(self):
        from django.core.management import get_commands

        assert get_commands()["db_worker"] == "core"

    @staticmethod
    def _worker():
        from core.management.commands.db_worker import Worker

        return Worker(
            queue_names=["*"],
            interval=1,
            batch=False,
            backend_name="default",
            startup_delay=False,
            max_tasks=None,
            worker_id="w",
            excluded_queue_names=[],
        )

    def test_its_worker_tells_the_running_task_of_a_shutdown_and_still_finishes_it(self, worker_shutdown):
        worker = self._worker()
        worker.running_task = True

        worker.shutdown(signal.SIGTERM, None)

        assert worker_shutdown.requested()
        assert worker.running is False

    def test_its_worker_keeps_saying_it_is_alive_while_it_runs(self, monkeypatch):
        from django_tasks_db.management.commands import db_worker as upstream

        from core import worker_liveness

        start, upstream_run = Mock(), Mock()
        monkeypatch.setattr(worker_liveness, "start", start)
        monkeypatch.setattr(upstream.Worker, "run", upstream_run)

        self._worker().run()

        start.assert_called_once_with("w")
        upstream_run.assert_called_once()

    def test_the_command_runs_that_worker(self, monkeypatch):
        from django_tasks_db.management.commands import db_worker as upstream

        from core.management.commands.db_worker import Command, Worker

        ran = Mock()
        monkeypatch.setattr(Command, "configure_logging", Mock())
        monkeypatch.setattr(Worker, "configure_signals", Mock())
        monkeypatch.setattr(Worker, "run", lambda worker: ran(type(worker)))

        Command().handle(
            verbosity=1,
            queue_name="*",
            interval=1,
            batch=True,
            backend_name="default",
            startup_delay=False,
            reload=False,
            max_tasks=None,
            worker_id="w",
            exclude_queues="",
        )

        ran.assert_called_once_with(Worker)
        assert upstream.Worker is not Worker


@pytest.mark.django_db
class TestSetupDefaultSite:
    def test_default_port_sets_the_bare_host(self, monkeypatch):
        monkeypatch.setattr(core_settings, "EXTERNAL_URL", HttpUrl("https://daiv.example.com"))
        call_command("setup_default_site")
        site = Site.objects.get(pk=settings.SITE_ID)
        assert site.domain == "daiv.example.com"
        assert site.name == "DAIV"

    def test_other_port_is_kept(self, monkeypatch):
        monkeypatch.setattr(core_settings, "EXTERNAL_URL", HttpUrl("https://daiv.example.com:8443"))
        call_command("setup_default_site")
        assert Site.objects.get(pk=settings.SITE_ID).domain == "daiv.example.com:8443"

    def test_missing_default_site_asks_for_migrate(self, monkeypatch):
        monkeypatch.setattr(core_settings, "EXTERNAL_URL", HttpUrl("https://daiv.example.com"))
        Site.objects.filter(pk=settings.SITE_ID).delete()
        with pytest.raises(CommandError, match="Run 'migrate' first"):
            call_command("setup_default_site")

    def test_url_without_host_is_rejected(self, monkeypatch):
        monkeypatch.setattr(core_settings, "EXTERNAL_URL", Mock(host=None, port=None))
        before = Site.objects.values("domain", "name").get(pk=settings.SITE_ID)
        with pytest.raises(CommandError, match="no valid host"):
            call_command("setup_default_site")
        # Nothing was written: the default row keeps its domain and name.
        assert Site.objects.values("domain", "name").get(pk=settings.SITE_ID) == before

    def test_prints_success_message(self, monkeypatch, capsys):
        monkeypatch.setattr(core_settings, "EXTERNAL_URL", HttpUrl("https://daiv.example.com:8443"))
        call_command("setup_default_site")
        assert "Default site updated: domain=daiv.example.com:8443, name=DAIV" in capsys.readouterr().out
