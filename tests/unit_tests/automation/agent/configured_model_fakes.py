from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

from automation.agent.model_spec import ModelSpecError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from automation.agent.notices import ModelFallbackNotice


class FakeModels:
    """A ``get_model`` stub: a ``Mock`` per spec in ``specs``, the given ``models``, ``ModelSpecError`` for others."""

    def __init__(self, *specs: str, models: Mapping[str, object] | None = None):
        self.by_spec = {**{spec: Mock(name=spec) for spec in specs}, **(models or {})}
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, *, model: str, thinking_level: str | None = None):
        self.calls.append((model, thinking_level))
        if model not in self.by_spec:
            raise ModelSpecError(f"Unknown provider prefix in '{model}'")
        return self.by_spec[model]


class FakeNotices:
    """A ``RunNotices`` that keeps what it records, and how many notices it held at each flush."""

    def __init__(self):
        self.recorded: list[ModelFallbackNotice] = []
        self.flushed_with: list[int] = []

    def record(self, notice: ModelFallbackNotice) -> None:
        self.recorded.append(notice)

    async def aflush(self) -> None:
        self.flushed_with.append(len(self.recorded))
