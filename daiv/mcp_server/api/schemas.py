from typing import Literal

from ninja import Field, Schema
from pydantic import model_serializer


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

    @model_serializer(mode="wrap")
    def _exclude_none_fields(self, handler):
        data = handler(self)
        return {k: v for k, v in data.items() if v is not None}
