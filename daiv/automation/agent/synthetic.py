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


def synthetic_message(content: str, *, kind: str) -> HumanMessage:
    """A ``HumanMessage`` marked as DAIV's own.

    Its id is set here, not by the ``add_messages`` reducer, so the copy sent on a model call and the copy
    saved into the thread are the same message.
    """
    return HumanMessage(content=content, id=str(uuid.uuid4()), additional_kwargs={SYNTHETIC_KWARG: kind})


def is_synthetic(message: Any) -> bool:
    """Whether ``message`` carries the synthetic mark. Dict-shaped checkpoint messages never do."""
    additional_kwargs = getattr(message, "additional_kwargs", None) or {}
    return bool(additional_kwargs.get(SYNTHETIC_KWARG))
