from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ag_ui.core.events import BaseEvent

    from .relay import RunRelay


async def publish_event(run_relay: RunRelay, event: BaseEvent) -> None:
    """Serialize one AG-UI event to the relay, stamped with its emit time.

    The chat client clocks a turn's reasoning segments from ``timestamp`` (epoch ms): a
    rejoining browser replays the whole run at once, so its own clock would measure every
    thought as instantaneous. Only this side knows when the event actually happened.
    """
    event.timestamp = event.timestamp or int(time.time() * 1000)
    await run_relay.publish_event(event.model_dump_json(by_alias=True, exclude_none=True))
