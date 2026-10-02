from pydantic import BaseModel

from core import checkpoint_types
from core.checkpoint_types import register_checkpoint_type, registered_checkpoint_types


class _Snapshot(BaseModel):
    note: str


def test_registering_a_type_twice_lists_it_once(monkeypatch):
    monkeypatch.setattr(checkpoint_types, "_registered", set())

    register_checkpoint_type(_Snapshot)
    register_checkpoint_type(_Snapshot)

    assert registered_checkpoint_types() == (_Snapshot,)
