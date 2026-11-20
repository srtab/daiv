from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.models import Item, ItemIn
from app.store import store

router = APIRouter()


class QuantityUpdate(BaseModel):
    quantity: int = Field(ge=0)


@router.get("")
def list_items() -> list[Item]:
    return store.list()


@router.post("", status_code=status.HTTP_201_CREATED)
def create_item(data: ItemIn) -> Item:
    return store.add(data)


@router.get("/{item_id}")
def get_item(item_id: int) -> Item:
    item = store.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@router.patch("/{item_id}")
def update_quantity(item_id: int, data: QuantityUpdate) -> Item:
    item = store.update_quantity(item_id, data.quantity)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(item_id: int) -> None:
    if not store.delete(item_id):
        raise HTTPException(status_code=404, detail="Item not found")
