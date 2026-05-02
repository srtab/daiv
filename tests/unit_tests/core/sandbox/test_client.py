import base64
from unittest.mock import MagicMock, patch

import httpx
import pytest
from pydantic import SecretStr


@pytest.fixture
def fake_settings():
    """Patch core.sandbox.client.settings and site_settings to avoid DB access."""
    with (
        patch("core.sandbox.client.settings") as core_settings_patch,
        patch("core.sandbox.client.site_settings") as site_settings_patch,
    ):
        url_mock = MagicMock()
        url_mock.unicode_string.return_value = "http://sandbox.test/"
        core_settings_patch.SANDBOX_URL = url_mock
        site_settings_patch.sandbox_api_key = SecretStr("fake-key")
        site_settings_patch.sandbox_timeout = 10
        yield


async def test_seed_session_posts_repo_archive(fake_settings, monkeypatch):
    from core.sandbox.client import DAIVSandboxClient

    captured: dict = {}

    async def fake_post(self, url, json):
        captured["url"] = url
        captured["json"] = json
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    await DAIVSandboxClient().seed_session("sid-123", repo_archive=b"tar-bytes")

    assert captured["url"] == "session/sid-123/seed/"
    assert captured["json"]["repo_archive"] == base64.b64encode(b"tar-bytes").decode()


async def test_seed_session_raises_on_409(fake_settings, monkeypatch):
    from core.sandbox.client import DAIVSandboxClient

    async def fake_post(self, url, json):
        return httpx.Response(409, json={"detail": "Session already seeded"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    with pytest.raises(httpx.HTTPStatusError):
        await DAIVSandboxClient().seed_session("sid-123", repo_archive=b"tar")
