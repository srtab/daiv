from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

if TYPE_CHECKING:
    from collections.abc import Mapping


class FakeModels:
    """A ``get_model`` stub: a ``Mock`` per spec in ``specs``, the given ``models``, ``ValueError`` for others."""

    def __init__(self, *specs: str, models: Mapping[str, object] | None = None):
        self.by_spec = {**{spec: Mock(name=spec) for spec in specs}, **(models or {})}
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, *, model: str, thinking_level: str | None = None):
        self.calls.append((model, thinking_level))
        if model not in self.by_spec:
            raise ValueError(f"Unknown provider prefix in '{model}'")
        return self.by_spec[model]
