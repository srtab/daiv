from typing import Literal

from ninja import Schema


class ModelSchema(Schema):
    id: str
    object: Literal["model"]
    created: int | None
    owned_by: str


class ModelListSchema(Schema):
    object: Literal["list"]
    data: list[ModelSchema]


class MessageSchema(Schema):
    role: Literal["user", "assistant", "system"]
    content: str
    name: str | None = None


class ChatCompletionRequest(Schema):
    model: str | None
    messages: list[MessageSchema]
    stream: bool = False


class ChatCompletionResponse(Schema):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    choices: list[dict[str, int | dict | str]]


class ChatCompletionChunk(Schema):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str | None = None
    choices: list[dict[str, int | dict | str | None]]
