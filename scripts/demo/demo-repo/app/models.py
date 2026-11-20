from pydantic import BaseModel, Field


class ItemIn(BaseModel):
    sku: str = Field(min_length=3, max_length=32)
    name: str
    quantity: int = Field(ge=0)


class Item(ItemIn):
    id: int
