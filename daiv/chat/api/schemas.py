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
