"""Messages DAIV adds to a thread on its own, as opposed to ones a person wrote.

They reach the model like any other ``HumanMessage``. The mark in ``additional_kwargs`` lets transcripts hide
them and lets turn-boundary checks look past them. Kept free of ``langchain`` imports beyond
``langchain_core.messages`` so ``sessions`` and the webhook managers can import it cheaply.
"""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.messages import HumanMessage

SYNTHETIC_KWARG = "daiv_synthetic"
ISSUE_CONTEXT_KIND = "issue_context"


def synthetic_message(content: str, *, kind: str, message_id: str | None = None) -> HumanMessage:
    """A ``HumanMessage`` marked as DAIV's own.

    Its id is set here, not by the ``add_messages`` reducer, so the copy sent on a model call and the copy
    saved into the thread are the same message. Pass ``message_id`` to make a re-sent message replace its
    earlier copy instead of adding a second one.
    """
    return HumanMessage(content=content, id=message_id or str(uuid.uuid4()), additional_kwargs={SYNTHETIC_KWARG: kind})


def synthetic_kind(message: Any) -> str | None:
    """The kind ``message`` was marked with, or ``None`` when it carries no mark."""
    additional_kwargs = getattr(message, "additional_kwargs", None) or {}
    return additional_kwargs.get(SYNTHETIC_KWARG) or None


def is_synthetic(message: Any) -> bool:
    """Whether ``message`` carries the synthetic mark. Dict-shaped checkpoint messages never do."""
    return synthetic_kind(message) is not None
