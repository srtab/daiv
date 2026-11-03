from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage
from sessions.inbox.drain import Drain
from sessions.inbox.render import RenderedInput


@pytest.fixture
def real_chat_inbox():
    """Opt a test out of ``chat_inbox``'s stubs, for the tests of that wiring."""


@pytest.fixture(autouse=True)
def chat_inbox(request):
    """Stub the session inbox around a chat turn: linking the events the view took, the context they render to,
    putting back the ones no run took over, and every drain. Most chat tests drive fake ``Run`` rows
    and no database; a test of that wiring asks for ``real_chat_inbox``."""
    if "real_chat_inbox" in request.fixturenames:
        yield None
        return
    with (
        patch("chat.api.streaming.ChatClaim", autospec=True) as claim,
        patch("chat.api.streaming.RunInput", autospec=True) as run_input_cls,
        patch.object(Drain, "start_logged", autospec=True) as drain,
    ):
        run_input = run_input_cls.return_value
        run_input.render.return_value = RenderedInput(context=None, request=HumanMessage(content=""))
        run_input_cls.aof_run.return_value = run_input
        yield SimpleNamespace(release_claim=claim.return_value.arelease, drain=drain)
