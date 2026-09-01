"""A chat turn that publishes a merge request must arm the CI watch.

The chat run is the one seam that cannot read the checkpoint at completion time — its cleanup
runs after ``open_checkpointer`` has closed — so ``published`` has to reach it on the AG-UI
``STATE_SNAPSHOT`` stream, the same way ``merge_request`` already does for the composer pill.
That is the whole reason ``GitState.published`` is public rather than a ``PrivateStateAttr``.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ag_ui.core import EventType

from chat.api.streaming import ChatRunStreamer
from tests.unit_tests.sessions.conftest import watch_recorder

MR = {"merge_request_id": 7, "source_branch": "daiv/chat-branch"}


def _snapshot_event(**values):
    return SimpleNamespace(type=EventType.STATE_SNAPSHOT, snapshot=values)


async def _drive(stream_events: list) -> list[dict]:
    """Run a whole turn over a canned AG-UI event stream and return the watch-arm calls."""
    armed: list[dict] = []

    @asynccontextmanager
    async def _fake_set_runtime_ctx(repo_id, **kwargs):
        yield MagicMock(config=MagicMock(models=MagicMock(agent=object())), repo=MagicMock(ref="daiv/chat-branch"))

    @asynccontextmanager
    async def _fake_open_checkpointer():
        yield MagicMock()

    class _FakeAguiAgent:
        def __init__(self, **kwargs):
            pass

        async def run(self, _input):
            for event in stream_events:
                yield event

    class _PassThroughFilter:
        def apply(self, stream):
            return stream

    streamer = ChatRunStreamer(
        repo_id="group/repo",
        ref="daiv/chat-branch",
        thread_id="t",
        run_id="r",
        input_data=MagicMock(thread_id="t", run_id="r"),
        user_id=5,
    )

    with (
        patch("chat.api.streaming.set_runtime_ctx", _fake_set_runtime_ctx),
        patch("chat.api.streaming.open_checkpointer", _fake_open_checkpointer),
        patch("chat.api.streaming.create_daiv_agent", AsyncMock(return_value=MagicMock())),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", _FakeAguiAgent),
        patch("chat.api.streaming.SubagentEventFilter", _PassThroughFilter),
        patch("chat.api.streaming.build_langsmith_config", return_value={}),
        patch("chat.api.streaming.get_daiv_agent_kwargs", return_value={"model_names": ["m"], "thinking_level": "low"}),
        patch("chat.api.streaming.start_chat_run", AsyncMock(return_value=None)),
        patch("chat.api.streaming.SessionLock", MagicMock(release=AsyncMock())),
        patch("chat.api.streaming.apersist_session_ref", AsyncMock()),
        patch("chat.api.streaming.PipelineWatch", watch_recorder(armed)),
    ):
        async for _event in streamer.events():
            pass

    return armed


@pytest.mark.django_db(transaction=True)
async def test_a_chat_turn_that_published_arms_the_watch():
    armed = await _drive([_snapshot_event(merge_request=MR, published=True)])

    assert len(armed) == 1
    assert armed[0]["repo_id"] == "group/repo"
    assert armed[0]["merge_request"] == MR
    assert armed[0]["published"] is True
    assert armed[0]["user_id"] == 5


@pytest.mark.django_db(transaction=True)
async def test_a_chat_turn_that_published_nothing_reports_it():
    """A turn that only answered a question still streams the MR it sits on, so the arm has to
    see ``published`` — otherwise chatting on a thread with a red pipeline starts a fix run."""
    armed = await _drive([_snapshot_event(merge_request=MR, published=False)])

    assert armed[0]["published"] is False


@pytest.mark.django_db(transaction=True)
async def test_the_last_snapshot_wins():
    """``published`` is per-turn and the publisher's snapshot is the final one; an earlier
    snapshot in the same turn must not decide it."""
    armed = await _drive([
        _snapshot_event(merge_request=None, published=False),
        _snapshot_event(merge_request=MR, published=True),
    ])

    assert armed[0]["published"] is True
    assert armed[0]["merge_request"] == MR


@pytest.mark.django_db(transaction=True)
async def test_a_failed_turn_does_not_arm():
    """Success-only, for the same reason ``apersist_session_ref`` is: a failed turn can have
    checked out a branch it never committed to."""
    armed = await _drive([
        _snapshot_event(merge_request=MR, published=True),
        SimpleNamespace(type=EventType.RUN_ERROR, message="boom", code="run_failed"),
    ])

    assert armed == []
