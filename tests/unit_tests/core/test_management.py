from unittest.mock import Mock

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.management import call_command
from django.core.management.base import CommandError

import pytest
from pydantic import HttpUrl

from core.conf import settings as core_settings


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
