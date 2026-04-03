from typing import Literal

from ninja import Field, Schema


class ClientRegistrationRequest(Schema):
    client_name: str = "MCP Client"
    redirect_uris: list[str] = Field(min_length=1)
    token_endpoint_auth_method: Literal["none", "client_secret_post"] = "none"  # noqa: S105


class ClientRegistrationResponse(Schema):
    client_id: str
    client_name: str
    redirect_uris: list[str]
    grant_types: list[str]
    response_types: list[str]
    token_endpoint_auth_method: str
    client_secret: str | None = None
