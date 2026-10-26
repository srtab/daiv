"""Messages DAIV adds to a thread on its own, as opposed to ones a person wrote.

They reach the model like any other ``HumanMessage``. The mark in ``additional_kwargs`` lets transcripts hide
them and lets turn-boundary checks look past them. Kept free of ``langchain`` imports beyond
``langchain_core.messages`` so ``sessions`` and the webhook managers can import it cheaply.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage

if TYPE_CHECKING:
    from collections.abc import Mapping

SYNTHETIC_KWARG = "daiv_synthetic"
SYNTHETIC_DATA_KWARG = "daiv_synthetic_data"
ISSUE_CONTEXT_KIND = "issue_context"


def synthetic_message(
    content: str, *, kind: str, message_id: str | None = None, data: Mapping[str, Any] | None = None
) -> HumanMessage:
    """A ``HumanMessage`` marked as DAIV's own.

    Its id is set here, not by the ``add_messages`` reducer, so the copy sent on a model call and the copy
    saved into the thread are the same message. Pass ``message_id`` to make a re-sent message replace its
    earlier copy instead of adding a second one. ``data`` rides along in ``additional_kwargs`` for the
    middleware that sent the message to read back later.
    """
    additional_kwargs: dict[str, Any] = {SYNTHETIC_KWARG: kind}
    if data is not None:
        additional_kwargs[SYNTHETIC_DATA_KWARG] = data
    return HumanMessage(content=content, id=message_id or str(uuid.uuid4()), additional_kwargs=additional_kwargs)


def synthetic_kind(message: Any) -> str | None:
    """The kind ``message`` was marked with, or ``None`` when it carries no mark."""
    additional_kwargs = getattr(message, "additional_kwargs", None) or {}
    return additional_kwargs.get(SYNTHETIC_KWARG) or None


def synthetic_data(message: Any) -> Mapping[str, Any] | None:
    """The ``data`` ``message`` was created with, or ``None`` when it carries none."""
    additional_kwargs = getattr(message, "additional_kwargs", None) or {}
    return additional_kwargs.get(SYNTHETIC_DATA_KWARG)


def is_synthetic(message: Any) -> bool:
    """Whether ``message`` carries the synthetic mark. Dict-shaped checkpoint messages never do."""
    return synthetic_kind(message) is not None


def is_person_message(message: Any) -> bool:
    """Whether ``message`` is a human message a person wrote, rather than one DAIV added."""
    return getattr(message, "type", None) == "human" and not is_synthetic(message)
