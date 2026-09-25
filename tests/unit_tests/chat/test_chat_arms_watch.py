"""A chat turn that publishes a merge request must arm the CI watch.

The executor arms it from the finished turn's checkpoint, as it does for every trigger. The AG-UI
``STATE_SNAPSHOT`` stream can't stand in: the adapter filters each snapshot to ``STREAMED_STATE_KEYS``, which
leaves ``published`` out, so a turn armed from the stream never armed at all.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ag_ui.core import EventType

from chat.api.streaming import ChatRunStreamer
from tests.unit_tests.sessions.executor.conftest import agent_stack

MR = {"merge_request_id": 7, "source_branch": "daiv/chat-branch"}


def _snapshot_event(**values):
    return SimpleNamespace(type=EventType.STATE_SNAPSHOT, snapshot=values)


async def _drive(stream_events: list, *, checkpoint: dict) -> list[dict]:
    """Run a whole turn over a canned AG-UI event stream that ends on ``checkpoint``; return the watch-arm calls."""

    class _FakeAguiAgent:
        def __init__(self, **kwargs):
            pass

        async def run(self, _input):
            for event in stream_events:
                yield event

    class _PassThroughFilter:
        def apply(self, stream):
            return stream

    graph = MagicMock(aget_state=AsyncMock(return_value=SimpleNamespace(values=checkpoint)))
    streamer = ChatRunStreamer(
        repo_id="group/repo",
        ref="daiv/chat-branch",
        thread_id="t",
        run_id="r",
        input_data=MagicMock(thread_id="t", run_id="r"),
        user_id=5,
    )

    with (
        agent_stack(graph, ctx=MagicMock(repo=SimpleNamespace(ref="daiv/chat-branch"))) as stack,
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", _FakeAguiAgent),
        patch("chat.api.streaming.SubagentEventFilter", _PassThroughFilter),
        patch("chat.api.streaming.start_chat_run", AsyncMock(return_value=None)),
        patch("chat.api.streaming.SessionLock", MagicMock(release=AsyncMock())),
    ):
        async for _event in streamer.events():
            pass

    return stack.armed


@pytest.mark.django_db(transaction=True)
async def test_a_chat_turn_that_published_arms_the_watch():
    armed = await _drive([_snapshot_event(merge_request=MR)], checkpoint={"merge_request": MR, "published": True})

    assert len(armed) == 1
    assert armed[0]["repo_id"] == "group/repo"
    assert armed[0]["merge_request"] == MR
    assert armed[0]["published"] is True
    assert armed[0]["user_id"] == 5


@pytest.mark.django_db(transaction=True)
async def test_a_chat_turn_that_published_nothing_reports_it():
    """A turn that only answered a question still sits on its MR, so the arm has to see ``published`` — otherwise
    chatting on a thread with a red pipeline starts a fix run."""
    armed = await _drive([_snapshot_event(merge_request=MR)], checkpoint={"merge_request": MR, "published": False})

    assert armed[0]["published"] is False


@pytest.mark.django_db(transaction=True)
async def test_the_checkpoint_decides_not_the_streamed_snapshots():
    armed = await _drive([_snapshot_event(merge_request=None)], checkpoint={"merge_request": MR, "published": True})

    assert armed[0]["published"] is True
    assert armed[0]["merge_request"] == MR


@pytest.mark.django_db(transaction=True)
async def test_a_failed_turn_does_not_arm():
    """Success-only, for the same reason ``apersist_session_ref`` is: a failed turn can have
    checked out a branch it never committed to."""
    armed = await _drive(
        [
            _snapshot_event(merge_request=MR),
            SimpleNamespace(type=EventType.RUN_ERROR, message="boom", code="run_failed"),
        ],
        checkpoint={"merge_request": MR, "published": True},
    )

    assert armed == []
