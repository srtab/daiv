from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def emit_custom_event():
    """Stub the sink ``streamed_assistant_message`` dispatches through.

    Shared so the patch target lives in one place: an ``assert_not_awaited()`` against a stale
    target passes vacuously, which is exactly the "message silently never streams" failure these
    tests exist to catch.
    """
    with patch("automation.agent.utils.adispatch_custom_event", AsyncMock()) as emit:
        yield emit
