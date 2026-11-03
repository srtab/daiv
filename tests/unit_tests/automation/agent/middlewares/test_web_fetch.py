from __future__ import annotations

import hashlib
from unittest.mock import patch

from django.core.cache import cache

import pytest
from langchain_core.messages import AIMessage
from pydantic import SecretStr

from automation.agent.middlewares import web_fetch as web_fetch_module


class _FakeModel:
    def __init__(self, content: str | list[dict]):
        self._content = content

    async def ainvoke(self, _messages):
        return AIMessage(content=self._content)


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
    assert result.content == "Hello, world!"


async def test_caches_full_response_by_url_and_prompt(httpx_mock):
    cache.clear()

    httpx_mock.add_response(
        url="https://example.com/page",
        status_code=200,
        headers={"content-type": "text/html"},
        text="<html><body><h1>Title</h1><p>Body</p></body></html>",
    )
    with (
        patch.object(web_fetch_module, "get_model") as mock_get_model,
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
    ):
        mock_site_settings.web_fetch_cache_ttl_seconds = 15 * 60
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None
        mock_site_settings.web_fetch_max_content_chars = 999_999
        mock_site_settings.web_fetch_model_name = "openrouter:openai/gpt-4.1-mini"

        mock_get_model.return_value = _FakeModel("ANSWER")

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
    mock_get_model.assert_called_once_with(
        model="openrouter:openai/gpt-4.1-mini", timeout=web_fetch_module.WEB_FETCH_MODEL_TIMEOUT_SECONDS
    )


async def test_summary_keeps_only_the_answer_text_of_content_blocks(httpx_mock):
    cache.clear()

    httpx_mock.add_response(
        url="https://example.com/page",
        status_code=200,
        headers={"content-type": "text/html"},
        text="<html><body><p>Body</p></body></html>",
    )
    blocks = [
        {"type": "reasoning", "summary": [], "encrypted_content": "gAAAAopaque"},
        {"type": "text", "text": "ANSWER", "phase": "final_answer"},
    ]
    with (
        patch.object(web_fetch_module, "get_model") as mock_get_model,
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
    ):
        mock_site_settings.web_fetch_cache_ttl_seconds = 15 * 60
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None
        mock_site_settings.web_fetch_max_content_chars = 999_999
        mock_site_settings.web_fetch_model_name = "openai:gpt-6-luna"
        mock_get_model.return_value = _FakeModel(blocks)

        result = await web_fetch_module.web_fetch_tool.ainvoke({"url": "https://example.com/page", "prompt": "P"})

    assert result == "ANSWER"
    assert web_fetch_module._get_cached_response(url="https://example.com/page", prompt="P") == "ANSWER"


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
        patch.object(web_fetch_module, "get_model") as mock_get_model,
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
    ):
        mock_site_settings.web_fetch_cache_ttl_seconds = 15 * 60
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None
        mock_site_settings.web_fetch_max_content_chars = 999_999
        mock_site_settings.web_fetch_model_name = "openrouter:openai/gpt-4.1-mini"

        mock_get_model.return_value = _FakeModel("ANSWER")

        await web_fetch_module.web_fetch_tool.ainvoke({"url": "https://example.com/page", "prompt": "P1"})
        await web_fetch_module.web_fetch_tool.ainvoke({"url": "https://example.com/page", "prompt": "P2"})

    assert len(httpx_mock.get_requests()) == 2


async def test_invalid_url_returns_message():
    result = await web_fetch_module.web_fetch_tool.ainvoke({"url": "not-a-url", "prompt": "x"})
    assert result == "error: Invalid URL. Provide a fully-formed http(s) URL (e.g., https://example.com)."


async def test_logs_the_outcome_of_each_call(httpx_mock, caplog):
    httpx_mock.add_response(url="https://example.com/gone", status_code=404)
    with (
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
        caplog.at_level("INFO", logger="daiv.tools"),
    ):
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None

        await web_fetch_module.web_fetch_tool.ainvoke({"url": "https://example.com/gone", "prompt": "x"})

    assert "outcome=fetch_error" in caplog.text
    assert "url=https://example.com/gone" in caplog.text


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


SPHINX_PAGE = """<html><body>
<nav><ul><li><a href="#features">Features</a></li><li><a href="#removed">Removed</a></li></ul></nav>
<section id="s-features"><span id="features"></span>
  <h2>Features<a class="headerlink" href="#features">¶</a></h2><p>new feature body</p>
  <section id="s-minor"><span id="minor"></span>
    <h3>Minor<a class="headerlink" href="#minor">¶</a></h3><p>minor body</p>
  </section>
</section>
<section id="s-removed"><span id="removed"></span>
  <h2>Removed<a class="headerlink" href="#removed">¶</a></h2><p>removed body</p>
</section>
</body></html>"""

GITHUB_PAGE = """<html><body><article class="markdown-body">
<div class="markdown-heading"><h2 class="heading-element">1.0.2</h2>
  <a class="anchor" href="#102" id="user-content-102"><svg></svg></a></div>
<p>release two body</p>
<div class="markdown-heading"><h2 class="heading-element">1.0.1</h2>
  <a class="anchor" href="#101" id="user-content-101"><svg></svg></a></div>
<p>release one body</p>
</article></body></html>"""


@pytest.mark.parametrize(
    ("page", "fragment", "kept", "dropped"),
    [
        pytest.param(SPHINX_PAGE, "features", ["new feature body", "minor body"], ["removed body"], id="sphinx"),
        pytest.param(SPHINX_PAGE, "minor", ["minor body"], ["new feature body", "removed body"], id="sphinx-sub"),
        pytest.param(SPHINX_PAGE, "s-removed", ["removed body"], ["new feature body"], id="container-id"),
        pytest.param(GITHUB_PAGE, "102", ["release two body"], ["release one body"], id="github-self-link"),
    ],
)
async def test_html_section_returns_only_the_named_section(page, fragment, kept, dropped):
    section = web_fetch_module._html_section(page, fragment)

    assert section is not None
    for text in kept:
        assert text in section
    for text in dropped:
        assert text not in section


async def test_html_section_ignores_links_that_only_point_at_the_fragment():
    page = '<html><body><a href="#usage">Usage</a><h2>Usage</h2><p>body</p></body></html>'

    assert web_fetch_module._html_section(page, "usage") is None


async def test_fragment_fetches_the_page_once_and_returns_only_that_section(httpx_mock):
    httpx_mock.add_response(
        url="https://docs.test/releases/", status_code=200, headers={"content-type": "text/html"}, text=SPHINX_PAGE
    )
    with (
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
    ):
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None
        mock_site_settings.web_fetch_max_content_chars = 999_999
        mock_site_settings.web_fetch_model_name = None

        result = await web_fetch_module.web_fetch_tool.ainvoke({
            "url": "https://docs.test/releases/#removed",
            "prompt": "",
        })

    assert "removed body" in result
    assert "new feature body" not in result
    assert [str(request.url) for request in httpx_mock.get_requests()] == ["https://docs.test/releases/"]


async def test_rejects_large_content_and_lists_section_anchors(httpx_mock):
    httpx_mock.add_response(
        url="https://docs.test/releases/", status_code=200, headers={"content-type": "text/html"}, text=SPHINX_PAGE
    )
    with (
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
    ):
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None
        mock_site_settings.web_fetch_max_content_chars = 5

        result = await web_fetch_module.web_fetch_tool.ainvoke({
            "url": "https://docs.test/releases/#missing",
            "prompt": "x",
        })

    assert result.startswith("error: Page content is too large to analyze in one pass")
    assert "#missing" in result
    assert "#features  Features\n#minor  Minor\n#removed  Removed" in result
    assert "narrow the prompt" not in result


async def test_rejects_large_text_content_without_anchors(httpx_mock):
    httpx_mock.add_response(
        url="https://example.com/CHANGES.rst", status_code=200, headers={"content-type": "text/plain"}, text="X" * 10
    )
    with (
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
    ):
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None
        mock_site_settings.web_fetch_max_content_chars = 5

        result = await web_fetch_module.web_fetch_tool.ainvoke({
            "url": "https://example.com/CHANGES.rst",
            "prompt": "x",
        })

    assert result.startswith("error: Page content is too large to analyze in one pass")
    assert "#fragment" not in result


async def test_get_auth_headers_exact_domain_match():
    with patch.object(web_fetch_module, "site_settings") as mock_site_settings:
        mock_site_settings.web_fetch_auth_headers = {"context7.com": {"X-API-Key": SecretStr("sk-abc")}}
        result = web_fetch_module._get_auth_headers_for_url("https://context7.com/api/v1/context")
    assert result == {"X-API-Key": "sk-abc"}


async def test_get_auth_headers_subdomain_match():
    with patch.object(web_fetch_module, "site_settings") as mock_site_settings:
        mock_site_settings.web_fetch_auth_headers = {"context7.com": {"X-API-Key": SecretStr("sk-abc")}}
        result = web_fetch_module._get_auth_headers_for_url("https://api.context7.com/endpoint")
    assert result == {}


async def test_get_auth_headers_no_match():
    with patch.object(web_fetch_module, "site_settings") as mock_site_settings:
        mock_site_settings.web_fetch_auth_headers = {"context7.com": {"X-API-Key": SecretStr("sk-abc")}}
        result = web_fetch_module._get_auth_headers_for_url("https://example.com/page")
    assert result == {}


async def test_get_auth_headers_rejects_false_suffix():
    with patch.object(web_fetch_module, "site_settings") as mock_site_settings:
        mock_site_settings.web_fetch_auth_headers = {"context7.com": {"X-API-Key": SecretStr("sk-abc")}}
        result = web_fetch_module._get_auth_headers_for_url("https://notcontext7.com/page")
    assert result == {}


async def test_get_auth_headers_more_specific_domain_wins():
    with patch.object(web_fetch_module, "site_settings") as mock_site_settings:
        mock_site_settings.web_fetch_auth_headers = {
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
        mock_site_settings.web_fetch_auth_headers = {"example.com": {"X-API-Key": SecretStr("sk-abc")}}
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
        patch.object(web_fetch_module, "get_model") as mock_get_model,
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
    ):
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None
        mock_site_settings.web_fetch_max_content_chars = 999_999
        mock_site_settings.web_fetch_model_name = "openrouter:openai/gpt-4.1-mini"

        mock_get_model.return_value = _FailingModel()

        result = await web_fetch_module.web_fetch_tool.ainvoke({"url": "https://example.com", "prompt": "x"})
    assert result == "Model processing failed (Boom). Contents of https://example.com:\nCONTENT"


async def test_model_build_failure_logs_error_and_still_returns_content(httpx_mock, caplog):
    httpx_mock.add_response(
        url="https://example.com",
        status_code=200,
        headers={"content-type": "text/html"},
        text="<html><body>CONTENT</body></html>",
    )
    with (
        patch.object(web_fetch_module, "get_model") as mock_get_model,
        patch.object(web_fetch_module, "site_settings") as mock_site_settings,
        patch.object(web_fetch_module, "automation_env_settings") as mock_env_settings,
    ):
        mock_site_settings.web_fetch_timeout_seconds = 1
        mock_env_settings.WEB_FETCH_PROXY_URL = None
        mock_site_settings.web_fetch_max_content_chars = 999_999
        mock_site_settings.web_fetch_model_name = "openrouter:openai/gpt-4.1-mini"
        mock_get_model.side_effect = RuntimeError("Provider 'openrouter' has no API key configured.")

        with caplog.at_level("ERROR", logger="daiv.tools"):
            result = await web_fetch_module.web_fetch_tool.ainvoke({"url": "https://example.com", "prompt": "x"})

    assert "summariser unavailable" in result
    assert "CONTENT" in result
    assert any(record.levelname == "ERROR" for record in caplog.records)
