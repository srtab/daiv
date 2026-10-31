from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage
from sessions.inbox.render import RenderedInput


@pytest.fixture
def real_chat_inbox():
    """Opt a test out of ``chat_inbox``'s stubs, for the tests of that wiring."""


@pytest.fixture(autouse=True)
def chat_inbox(request):
    """Stub the session inbox around a chat turn: linking the events the view took, the context they render to and
    the drain after the turn. Most chat tests drive fake ``Run`` rows and no database; a test of that wiring asks for
    ``real_chat_inbox``."""
    if "real_chat_inbox" in request.fixturenames:
        yield None
        return
    rendered = RenderedInput(context=None, request=HumanMessage(content=""))
    with (
        patch("chat.api.streaming.alink_chat_run", new=AsyncMock()) as link,
        patch("chat.api.streaming.arender_input", new=AsyncMock(return_value=rendered)) as render,
        patch("chat.api.streaming.drain_logged", new=MagicMock()) as drain,
    ):
        yield SimpleNamespace(link=link, render=render, drain=drain)
