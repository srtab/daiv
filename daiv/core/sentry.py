from __future__ import annotations

import traceback
from typing import TYPE_CHECKING

from pydantic import ValidationError

if TYPE_CHECKING:
    from sentry_sdk.types import Event, Hint


def before_send(event: Event, hint: Hint) -> Event | None:
    """Drop invalid tool arguments, which the agent's ``ToolNode`` turns into an error ``ToolMessage`` to retry on.

    langchain fires ``on_tool_error`` for them before the ``ToolNode`` handles them, so the Sentry integration
    records each one as unhandled. A ``ValidationError`` raised by a tool's own code is still reported.
    """
    exc_info = hint.get("exc_info")
    if exc_info and _is_tool_argument_error(exc_info[1]):
        return None
    return event


def _is_tool_argument_error(exc: BaseException | None) -> bool:
    return isinstance(exc, ValidationError) and any(
        frame.f_code.co_name == "_parse_input" and frame.f_globals.get("__name__") == "langchain_core.tools.base"
        for frame, _lineno in traceback.walk_tb(exc.__traceback__)
    )
