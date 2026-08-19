import ipaddress
from typing import Any, Literal
from urllib.parse import urlparse

from ninja import Field, Schema
from pydantic import SerializerFunctionWrapHandler, field_validator, model_serializer


def _is_loopback_host(host: str) -> bool:
    """Whether a redirect-URI host is a loopback target per RFC 8252.

    Plain ``http`` redirect URIs are only safe for loopback (used by local
    desktop/mobile clients). Accept the ``localhost`` name and any loopback IP
    literal (127.0.0.0/8, ::1); everything else must use ``https``."""
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback


class ClientRegistrationRequest(Schema):
    client_name: str = Field(default="MCP Client", min_length=1, max_length=255)
    redirect_uris: list[str] = Field(min_length=1)
    token_endpoint_auth_method: Literal["none", "client_secret_post"] = "none"  # noqa: S105

    @field_validator("redirect_uris")
    @classmethod
    def validate_redirect_uris(cls, v: list[str]) -> list[str]:
        for uri in v:
            if not uri.strip():
                raise ValueError("Redirect URIs must not be empty.")
            parsed = urlparse(uri)
            if parsed.scheme not in ("http", "https"):
                raise ValueError(
                    f"Unsupported redirect URI scheme: {parsed.scheme!r}. Only http and https are allowed."
                )
            # RFC 8252: plain http is only permitted for loopback (local clients);
            # any other host must register an https URI to protect the auth code.
            if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname or ""):
                raise ValueError(
                    "Plain http redirect URIs are only allowed for loopback "
                    "(localhost, 127.0.0.1, [::1]); use https for all other hosts."
                )
        return v


class ClientRegistrationResponse(Schema):
    client_id: str
    client_name: str
    redirect_uris: list[str]
    grant_types: list[str]
    response_types: list[str]
    token_endpoint_auth_method: str
    client_secret: str | None = None

    @model_serializer(mode="wrap")
    def _exclude_none_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        return {k: v for k, v in data.items() if v is not None}
