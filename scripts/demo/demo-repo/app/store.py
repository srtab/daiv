from app.models import Item, ItemIn


class ItemStore:
    def __init__(self) -> None:
        self._items: dict[int, Item] = {}
        self._next_id = 1

    def list(self) -> list[Item]:
        return list(self._items.values())

    def get(self, item_id: int) -> Item | None:
        return self._items.get(item_id)

    def add(self, data: ItemIn) -> Item:
        item = Item(id=self._next_id, **data.model_dump())
        self._items[item.id] = item
        self._next_id += 1
        return item

    def update_quantity(self, item_id: int, quantity: int) -> Item | None:
        item = self._items.get(item_id)
        if item is None:
            return None
        item.quantity = quantity
        return item

    def delete(self, item_id: int) -> bool:
        return self._items.pop(item_id, None) is not None


store = ItemStore()
