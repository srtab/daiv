"""Shared row→SDK-client kwargs primitive.

Both :func:`automation.agent.model_catalog.adapters` and
:func:`automation.agent.base.get_model_kwargs` resolve the same per-row
inputs (api_key plaintext, base_url, extra headers, optional verify_ssl-aware
httpx client). This module owns that resolution; consumers layer on their
own shape on top (LangChain ``init_chat_model`` kwargs vs. raw SDK client
constructor kwargs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from automation.agent.model_catalog.exceptions import MissingApiKeyError

if TYPE_CHECKING:
    import httpx

    from core.models import Provider


class SdkClientKwargs(TypedDict):
    api_key: str
    base_url: str | None
    default_headers: dict[str, str]
    http_client: httpx.AsyncClient | None


def build_sdk_client_kwargs(row: Provider.Cached) -> SdkClientKwargs:
    """Return SDK-client kwargs for a provider row.

    Raises :class:`MissingApiKeyError` if the row has no API key. The caller
    fills in the provider-specific default ``base_url`` when this returns
    ``None``, and is responsible for closing ``http_client`` (use
    ``async with`` in adapters).
    """
    if row.api_key is None:
        raise MissingApiKeyError(f"Provider '{row.slug}' has no API key configured.")

    http_client = None
    if not row.verify_ssl:
        import httpx

        # admin-opted-in via Provider.verify_ssl; matches base.py:_apply_insecure_http_clients
        http_client = httpx.AsyncClient(verify=False)  # noqa: S501

    return SdkClientKwargs(
        api_key=row.api_key.get_secret_value(),
        base_url=row.base_url or None,
        default_headers=dict(row.extra_headers or {}),
        http_client=http_client,
    )
