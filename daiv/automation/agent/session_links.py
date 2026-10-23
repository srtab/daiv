"""The links a publish puts in its commit and merge request back to the session it comes from.

Kept free of the agent stack so the run executor can build them without loading it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass(frozen=True)
class SessionLinks:
    """Absolute URLs of the session a publish comes from: ``detail`` goes in the commit's session trailer,
    ``merge_request`` in the merge request description."""

    detail: str
    merge_request: str


type SessionLinksResolver = Callable[[], Awaitable[SessionLinks | None]]
"""Builds the run's ``SessionLinks`` when a publish has something to push; ``None`` when they can't be built."""
