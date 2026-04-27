"""Direct unit tests for ``ChatRunStreamer.events()``.

The HTTP-level tests in test_views.py exercise the empty-stream and
exception paths. These tests cover the streaming-specific behavior the HTTP
tests don't reach — most importantly the STATE_SNAPSHOT-driven ``last_mr``
capture that keeps the composer MR pill alive across reloads.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ag_ui.core.events import EventType, StateSnapshotEvent
from ag_ui.encoder import EventEncoder

from chat.api.streaming import ChatRunStreamer


def _mock_ctx(*_args, **_kwargs):
    """Async context manager yielding a MagicMock — used to stand in for
    ``open_checkpointer`` / ``set_runtime_ctx`` so we don't touch Redis or
    clone a repo.
    """
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=MagicMock())
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _streamer(input_data) -> ChatRunStreamer:
    return ChatRunStreamer(
        repo_id="a/b",
        ref="main",
        thread_id="t-stream",
        run_id="r-1",
        input_data=input_data,
        encoder=EventEncoder(accept="text/event-stream"),
    )


def _mock_agent(events):
    """Patch ``RuntimeContextLangGraphAGUIAgent`` so its instance's ``run()``
    yields the supplied iterable of AGUI events.
    """

    async def _run(_input):
        for e in events:
            yield e

    instance = MagicMock()
    instance.run = _run
    return instance


@pytest.mark.django_db(transaction=True)
async def test_events_captures_merge_request_from_state_snapshot_and_persists_ref():
    mr = SimpleNamespace(source_branch="feature-y")
    snapshot = StateSnapshotEvent(
        type=EventType.STATE_SNAPSHOT,
        raw_event={"metadata": {"langgraph_checkpoint_ns": ""}},
        snapshot={"merge_request": mr},
    )

    persist_calls = []
    release_calls = []

    async def _capture_persist(thread_id, original_ref, captured_mr):
        persist_calls.append((thread_id, original_ref, captured_mr))

    async def _capture_release(thread_id):
        release_calls.append(thread_id)

    with (
        patch("chat.api.streaming.open_checkpointer", _mock_ctx),
        patch("chat.api.streaming.set_runtime_ctx", _mock_ctx),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([snapshot])),
        patch("chat.api.streaming.ChatThreadService.persist_ref", side_effect=_capture_persist),
        patch("chat.api.streaming.ChatThreadService.release_run", side_effect=_capture_release),
    ):
        streamer = _streamer(input_data=MagicMock())
        # Drain the generator — we don't assert on encoded payloads here, just
        # that the side-effects fire with the captured MR.
        async for _ in streamer.events():
            pass

    assert persist_calls == [("t-stream", "main", mr)]
    assert release_calls == ["t-stream"]


@pytest.mark.django_db(transaction=True)
async def test_events_persists_none_when_no_state_snapshot_carries_merge_request():
    # A run that never emits a snapshot with ``merge_request`` should leave the
    # thread's ref untouched. We assert this via persist_ref receiving None.
    snapshot_no_mr = StateSnapshotEvent(
        type=EventType.STATE_SNAPSHOT,
        raw_event={"metadata": {"langgraph_checkpoint_ns": ""}},
        snapshot={"messages": []},
    )

    persist_calls = []

    async def _capture_persist(thread_id, original_ref, captured_mr):
        persist_calls.append((thread_id, original_ref, captured_mr))

    with (
        patch("chat.api.streaming.open_checkpointer", _mock_ctx),
        patch("chat.api.streaming.set_runtime_ctx", _mock_ctx),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([snapshot_no_mr])),
        patch("chat.api.streaming.ChatThreadService.persist_ref", side_effect=_capture_persist),
        patch("chat.api.streaming.ChatThreadService.release_run", new=AsyncMock()),
    ):
        streamer = _streamer(input_data=MagicMock())
        async for _ in streamer.events():
            pass

    assert persist_calls == [("t-stream", "main", None)]


@pytest.mark.django_db(transaction=True)
async def test_events_releases_run_even_when_persist_ref_raises():
    # Regression: a DB hiccup in persist_ref must not leave the per-thread
    # slot permanently claimed. The finally block wraps both cleanup steps
    # independently so release_run still runs.
    release_calls = []

    async def _persist_boom(*_a, **_kw):
        raise RuntimeError("db down")

    async def _capture_release(thread_id):
        release_calls.append(thread_id)

    with (
        patch("chat.api.streaming.open_checkpointer", _mock_ctx),
        patch("chat.api.streaming.set_runtime_ctx", _mock_ctx),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([])),
        patch("chat.api.streaming.ChatThreadService.persist_ref", side_effect=_persist_boom),
        patch("chat.api.streaming.ChatThreadService.release_run", side_effect=_capture_release),
    ):
        streamer = _streamer(input_data=MagicMock())
        async for _ in streamer.events():
            pass

    assert release_calls == ["t-stream"]
