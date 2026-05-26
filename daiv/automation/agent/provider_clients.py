"""Shared row→SDK-client kwargs primitive. Consumers layer their own shape on top
(LangChain ``init_chat_model`` kwargs vs. raw SDK client constructor kwargs)."""

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


def build_sdk_client_kwargs(row: Provider.Cached, *, with_http_client: bool = True) -> SdkClientKwargs:
    """Return SDK-client kwargs for a provider row.

    Raises :class:`MissingApiKeyError` if the row has no API key. The caller
    fills in the provider-specific default ``base_url`` when this returns ``None``
    and is responsible for closing ``http_client``.

    Set ``with_http_client=False`` when the caller needs to construct both sync
    and async httpx clients itself — building one here would leak.
    """
    if row.api_key is None:
        raise MissingApiKeyError(f"Provider '{row.slug}' has no API key configured.")

    http_client = None
    if with_http_client and not row.verify_ssl:
        import httpx

        http_client = httpx.AsyncClient(verify=False)  # noqa: S501  # admin-opted-in via Provider.verify_ssl

    return SdkClientKwargs(
        api_key=row.api_key.get_secret_value(),
        base_url=row.base_url or None,
        default_headers=dict(row.extra_headers or {}),
        http_client=http_client,
    )
