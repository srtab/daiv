from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage
from sessions.inbox.render import RenderedInput, RunInput


@pytest.fixture
def real_chat_inbox():
    """Opt a test out of ``chat_inbox``'s stubs, for the tests of that wiring."""


@pytest.fixture(autouse=True)
def chat_inbox(request):
    """Stub the session inbox around a chat turn: linking the events the view took, the context they render to,
    putting back the ones no run took over, and the drain after the turn. Most chat tests drive fake ``Run`` rows
    and no database; a test of that wiring asks for ``real_chat_inbox``."""
    if "real_chat_inbox" in request.fixturenames:
        yield None
        return
    rendered = RenderedInput(context=None, request=HumanMessage(content=""))
    run_input = MagicMock(spec=RunInput)
    run_input.render.return_value = rendered
    with (
        patch("chat.api.streaming.ChatClaim", autospec=True) as claim,
        patch("chat.api.streaming.RunInput") as run_input_cls,
        patch("chat.api.streaming.Drain") as drain,
    ):
        run_input_cls.aof_run = AsyncMock(return_value=run_input)
        yield SimpleNamespace(
            link=claim.return_value.alink,
            render=run_input.render,
            release_claim=claim.return_value.arelease,
            drain=drain,
        )
