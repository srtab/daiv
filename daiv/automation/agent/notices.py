"""Notices a run records for its session view, through a ``RunNotices`` the run executor injects."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, Protocol

FallbackReason = Literal["unknown_provider", "provider_unavailable", "init_failed"]
NoticeTarget = Literal["skill", "subagent"]


@dataclass(frozen=True, kw_only=True)
class ModelFallbackNotice:
    """A configured model that could not be used, and the model that served instead. Never carries exception text."""

    target: NoticeTarget
    name: str
    configured_model: str
    served_model: str
    reason: FallbackReason
    kind: Literal["model_fallback"] = "model_fallback"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


class RunNotices(Protocol):
    def record(self, notice: ModelFallbackNotice) -> None:
        """Buffer ``notice``; synchronous so the synchronous subagent loaders can call it."""
        ...

    async def aflush(self) -> None:
        """Save every notice buffered so far."""
        ...
