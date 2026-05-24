"""Tests for the shared row→SDK-client kwargs helper."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from automation.agent.provider_clients import build_sdk_client_kwargs
from core.models import Provider, ProviderType


def _cached(
    *,
    slug: str = "openrouter",
    provider_type: ProviderType = ProviderType.OPENROUTER,
    base_url: str = "",
    api_key: str | None = "sk-test",
    extra_headers: dict | None = None,
    verify_ssl: bool = True,
) -> Provider.Cached:
    return Provider.Cached(
        slug=slug,
        display_name=slug.title(),
        provider_type=provider_type,
        base_url=base_url,
        api_key=SecretStr(api_key) if api_key else None,
        extra_headers=extra_headers or {},
        is_enabled=True,
        use_responses_api=False,
        verify_ssl=verify_ssl,
        is_locked=False,
        sort_order=0,
    )


def test_returns_api_key_plaintext():
    kw = build_sdk_client_kwargs(_cached(api_key="sk-abc"))
    assert kw["api_key"] == "sk-abc"


def test_base_url_none_when_unset():
    assert build_sdk_client_kwargs(_cached(base_url=""))["base_url"] is None


def test_base_url_preserved_when_set():
    kw = build_sdk_client_kwargs(_cached(base_url="https://proxy.example/v1"))
    assert kw["base_url"] == "https://proxy.example/v1"


def test_default_headers_from_extra_headers():
    kw = build_sdk_client_kwargs(_cached(extra_headers={"X-Foo": "bar"}))
    assert kw["default_headers"] == {"X-Foo": "bar"}


def test_default_headers_empty_when_no_extras():
    assert build_sdk_client_kwargs(_cached())["default_headers"] == {}


def test_http_client_none_when_verify_ssl_true():
    assert build_sdk_client_kwargs(_cached(verify_ssl=True))["http_client"] is None


def test_http_client_returned_when_verify_ssl_false():
    kw = build_sdk_client_kwargs(_cached(verify_ssl=False))
    assert isinstance(kw["http_client"], httpx.AsyncClient)
    # Caller owns lifecycle; close to avoid ResourceWarning.
    import asyncio

    asyncio.run(kw["http_client"].aclose())


def test_missing_api_key_raises():
    from automation.agent.model_catalog.exceptions import MissingApiKeyError

    with pytest.raises(MissingApiKeyError):
        build_sdk_client_kwargs(_cached(api_key=None))
