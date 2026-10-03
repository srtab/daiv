import importlib

import pytest


@pytest.fixture
def sentry_settings(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    return importlib.import_module("daiv.settings.components.sentry")


@pytest.mark.parametrize("key", ["access_token", "refresh_token", "client_secret"])
def test_the_platform_credential_keys_are_scrubbed(sentry_settings, key):
    assert key in sentry_settings.SENTRY_EXTRA_SCRUB_KEYS
