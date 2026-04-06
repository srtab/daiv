from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache

import pytest
from pydantic import SecretStr

from automation.agent.middlewares import web_fetch as web_fetch_module


class _FakeModel:
    def __init__(self, response_text: str):
        self._response_text = response_text

    async def ainvoke(self, _messages):
        return SimpleNamespace(content=self._response_text)


async def test_upgrade_http_to_https():
    assert web_fetch_module._upgrade_http_to_https("http://example.com") == "https://example.com"
    assert web_fetch_module._upgrade_http_to_https("https://example.com") == "https://example.com"
    assert (
        web_fetch_module._upgrade_http_to_https("https://example.com/path?query=value")
        == "https://example.com/path?query=value"
    )
    assert (
        web_fetch_module._upgrade_http_to_https("https://example.com/path?query=value#fragment")
        == "https://example.com/path?query=value#fragment"
    )


async def test_is_valid_http_url():
    assert web_fetch_module._is_valid_http_url("http://example.com")
    assert web_fetch_module._is_valid_http_url("https://example.com")
    assert web_fetch_module._is_valid_http_url("https://example.com/path?query=value")
    assert web_fetch_module._is_valid_http_url("https://example.com/path?query=value#fragment")
    assert not web_fetch_module._is_valid_http_url("not-a-url")
    assert not web_fetch_module._is_valid_http_url("file:///local/path")
    assert not web_fetch_module._is_valid_http_url("ftp://example.com")


async def test_fetch_url_text(httpx_mock):
    httpx_mock.add_response(
        url="https://example.com",
        status_code=200,
        headers={"content-type": "text/html"},
        text="<html><body>Hello, world!</body></html>",
    )
    result = await web_fetch_module._fetch_url_text("https://example.com", timeout_seconds=1, proxy_url=None)
    assert result == ("https://example.com", "text/html", "<html><body>Hello, world!</body></html>")


async def test_fetch_url_text_cross_host_redirect_returns_special_tag(httpx_mock):
    httpx_mock.add_response(url="https://site.test", status_code=302, headers={"location": "https://other.test/path"})
    with (
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
    ):
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None

        result = await web_fetch_module.web_fetch_tool.ainvoke({"url": "https://site.test", "prompt": "x"})
    assert result == "<redirect_url>https://other.test/path</redirect_url>"


async def test_fetch_url_text_same_host_redirect_is_followed(httpx_mock):
    httpx_mock.add_response(url="https://site.test", status_code=302, headers={"location": "/new-path"})
    httpx_mock.add_response(
        url="https://site.test/new-path",
        status_code=200,
        headers={"content-type": "text/html"},
        text="<html><body>Redirected</body></html>",
    )
    with (
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
    ):
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None
        mock_site_settings.web_fetch_max_content_chars = 999_999
        result = await web_fetch_module.web_fetch_tool.ainvoke({"url": "https://site.test", "prompt": ""})
    assert result == "Contents of https://site.test:\nRedirected"


async def test_fetch_url_text_same_host_redirect_depth_limit(httpx_mock):
    for i in range(web_fetch_module._MAX_REDIRECTS + 1):
        httpx_mock.add_response(url=f"https://site.test/{i}", status_code=302, headers={"location": f"/{i + 1}"})

    with pytest.raises(ValueError, match="Too many redirects"):
        await web_fetch_module._fetch_url_text("https://site.test/0", timeout_seconds=1, proxy_url=None)


async def test_cache_key_for_response():
    assert (
        web_fetch_module._cache_key_for_response(url="https://example.com", prompt="x")
        == f"web_fetch:response:{hashlib.sha256(b'https://example.com\nx').hexdigest()}"
    )


async def test_get_cached_response():
    cache.clear()

    cache.set(web_fetch_module._cache_key_for_response(url="https://example.com", prompt="x"), "ANSWER")
    assert web_fetch_module._get_cached_response(url="https://example.com", prompt="x") == "ANSWER"
    assert web_fetch_module._get_cached_response(url="https://example.com", prompt="y") is None


async def test_set_cached_response():
    cache.clear()

    web_fetch_module._set_cached_response(url="https://example.com", prompt="x", response="ANSWER")
    assert cache.get(web_fetch_module._cache_key_for_response(url="https://example.com", prompt="x")) == "ANSWER"


async def test_fetch_markdown_for_url(httpx_mock):
    httpx_mock.add_response(
        url="https://example.com",
        status_code=200,
        headers={"content-type": "text/html"},
        text="<html><body>Hello, world!</body></html>",
    )
    with (
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
    ):
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None

        result = await web_fetch_module._fetch_markdown_for_url("https://example.com")
    assert result == "Hello, world!"


async def test_caches_full_response_by_url_and_prompt(httpx_mock):
    cache.clear()

    httpx_mock.add_response(
        url="https://example.com/page",
        status_code=200,
        headers={"content-type": "text/html"},
        text="<html><body><h1>Title</h1><p>Body</p></body></html>",
    )
    with (
        patch.object(web_fetch_module, "BaseAgent") as mock_base_agent,
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
    ):
        mock_site_settings.web_fetch_cache_ttl_seconds = 15 * 60
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None
        mock_site_settings.web_fetch_max_content_chars = 999_999
        mock_site_settings.web_fetch_model_name = "openrouter:openai/gpt-4.1-mini"

        mock_base_agent.get_model.return_value = _FakeModel("ANSWER")

        result1 = await web_fetch_module.web_fetch_tool.ainvoke({
            "url": "https://example.com/page",
            "prompt": "Summarize",
        })
        result2 = await web_fetch_module.web_fetch_tool.ainvoke({
            "url": "https://example.com/page",
            "prompt": "Summarize",
        })

    assert result1 == "ANSWER"
    assert result2 == "ANSWER"
    assert httpx_mock.get_requests()


async def test_cache_key_changes_with_prompt(httpx_mock):
    cache.clear()

    httpx_mock.add_response(
        url="https://example.com/page",
        status_code=200,
        headers={"content-type": "text/html"},
        text="<html><body><h1>Title</h1><p>Body</p></body></html>",
    )
    httpx_mock.add_response(
        url="https://example.com/page",
        status_code=200,
        headers={"content-type": "text/html"},
        text="<html><body><h1>Title</h1><p>Body</p></body></html>",
    )
    with (
        patch.object(web_fetch_module, "BaseAgent") as mock_base_agent,
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
    ):
        mock_site_settings.web_fetch_cache_ttl_seconds = 15 * 60
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None
        mock_site_settings.web_fetch_max_content_chars = 999_999
        mock_site_settings.web_fetch_model_name = "openrouter:openai/gpt-4.1-mini"

        mock_base_agent.get_model.return_value = _FakeModel("ANSWER")

        await web_fetch_module.web_fetch_tool.ainvoke({"url": "https://example.com/page", "prompt": "P1"})
        await web_fetch_module.web_fetch_tool.ainvoke({"url": "https://example.com/page", "prompt": "P2"})

    assert len(httpx_mock.get_requests()) == 2


async def test_invalid_url_returns_message():
    result = await web_fetch_module.web_fetch_tool.ainvoke({"url": "not-a-url", "prompt": "x"})
    assert result == "Invalid URL. Provide a fully-formed http(s) URL (e.g., https://example.com)."


async def test_empty_prompt_returns_contents(httpx_mock):
    httpx_mock.add_response(
        url="https://example.com",
        status_code=200,
        headers={"content-type": "text/html"},
        text="<html><body>CONTENT</body></html>",
    )
    with (
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
    ):
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None
        mock_site_settings.web_fetch_max_content_chars = 999_999
        mock_site_settings.web_fetch_model_name = None

        result = await web_fetch_module.web_fetch_tool.ainvoke({"url": "https://example.com", "prompt": ""})
    assert result == "Contents of https://example.com:\nCONTENT"


async def test_rejects_large_content(httpx_mock):
    httpx_mock.add_response(
        url="https://example.com",
        status_code=200,
        headers={"content-type": "text/html"},
        text="<html><body>XXXXXXXXXX</body></html>",
    )
    with (
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
    ):
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None
        mock_site_settings.web_fetch_max_content_chars = 5

        result = await web_fetch_module.web_fetch_tool.ainvoke({"url": "https://example.com", "prompt": "x"})
    assert "Page content is too large to safely analyze in one pass." in result


async def test_get_auth_headers_exact_domain_match():
    with patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings:
        mock_env_settings.WEB_FETCH_AUTH_HEADERS = {"context7.com": {"X-API-Key": SecretStr("sk-abc")}}
        result = web_fetch_module._get_auth_headers_for_url("https://context7.com/api/v1/context")
    assert result == {"X-API-Key": "sk-abc"}


async def test_get_auth_headers_subdomain_match():
    with patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings:
        mock_env_settings.WEB_FETCH_AUTH_HEADERS = {"context7.com": {"X-API-Key": SecretStr("sk-abc")}}
        result = web_fetch_module._get_auth_headers_for_url("https://api.context7.com/endpoint")
    assert result == {}


async def test_get_auth_headers_no_match():
    with patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings:
        mock_env_settings.WEB_FETCH_AUTH_HEADERS = {"context7.com": {"X-API-Key": SecretStr("sk-abc")}}
        result = web_fetch_module._get_auth_headers_for_url("https://example.com/page")
    assert result == {}


async def test_get_auth_headers_rejects_false_suffix():
    with patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings:
        mock_env_settings.WEB_FETCH_AUTH_HEADERS = {"context7.com": {"X-API-Key": SecretStr("sk-abc")}}
        result = web_fetch_module._get_auth_headers_for_url("https://notcontext7.com/page")
    assert result == {}


async def test_get_auth_headers_more_specific_domain_wins():
    with patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings:
        mock_env_settings.WEB_FETCH_AUTH_HEADERS = {
            "example.com": {"X-API-Key": SecretStr("generic")},
            "api.example.com": {"X-API-Key": SecretStr("specific")},
        }
        result = web_fetch_module._get_auth_headers_for_url("https://api.example.com/v1")
    assert result == {"X-API-Key": "specific"}


async def test_fetch_url_text_injects_auth_headers(httpx_mock):
    httpx_mock.add_response(
        url="https://example.com/api/v1/context",
        status_code=200,
        headers={"content-type": "application/json"},
        text='{"result": "ok"}',
    )
    with (
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
    ):
        mock_env_settings.WEB_FETCH_AUTH_HEADERS = {"example.com": {"X-API-Key": SecretStr("sk-abc")}}
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None
        mock_site_settings.web_fetch_max_content_chars = 999_999
        mock_site_settings.web_fetch_model_name = None

        result = await web_fetch_module.web_fetch_tool.ainvoke({
            "url": "https://example.com/api/v1/context",
            "prompt": "",
        })

    sent_requests = httpx_mock.get_requests()
    assert len(sent_requests) == 1
    assert sent_requests[0].headers["X-API-Key"] == "sk-abc"
    assert "example.com" in result


async def test_model_failure_returns_contents(httpx_mock):
    class _FailingModel:
        async def ainvoke(self, _messages):
            raise RuntimeError("Boom")

    httpx_mock.add_response(
        url="https://example.com",
        status_code=200,
        headers={"content-type": "text/html"},
        text="<html><body>CONTENT</body></html>",
    )
    with (
        patch.object(web_fetch_module, "BaseAgent") as mock_base_agent,
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
    ):
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None
        mock_site_settings.web_fetch_max_content_chars = 999_999
        mock_site_settings.web_fetch_model_name = "openrouter:openai/gpt-4.1-mini"

        mock_base_agent.get_model.return_value = _FailingModel()

        result = await web_fetch_module.web_fetch_tool.ainvoke({"url": "https://example.com", "prompt": "x"})
    assert result == "Model processing failed (Boom). Contents of https://example.com:\nCONTENT"
