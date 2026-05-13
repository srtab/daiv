"""Direct unit tests for ``ChatRunStreamer.events()``.

Covers the streaming-specific behavior the HTTP-level tests in test_views.py
don't reach — most importantly the STATE_SNAPSHOT-driven ``last_mr`` capture
that keeps the composer MR pill alive across reloads, and the run-slot
lifecycle invariants.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ag_ui.core.events import EventType, StateSnapshotEvent
from ag_ui.encoder import EventEncoder

from chat.api.streaming import ChatRunStreamer


def _mock_ctx(*_args, **_kwargs):
    """Async context manager yielding a MagicMock — stands in for ``open_checkpointer``
    / ``set_runtime_ctx`` so we don't touch Redis or clone a repo.
    """
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=MagicMock())
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _streamer(input_data=None) -> ChatRunStreamer:
    if input_data is None:
        input_data = SimpleNamespace(thread_id="t-stream", run_id="r-1")
    return ChatRunStreamer(
        repo_id="a/b",
        ref="main",
        thread_id="t-stream",
        run_id="r-1",
        input_data=input_data,
        encoder=EventEncoder(accept="text/event-stream"),
    )


def _mock_agent(events):
    """Patch ``RuntimeContextLangGraphAGUIAgent`` so its instance's ``run()`` yields
    the supplied iterable of AGUI events.
    """

    async def _run(_input):
        for e in events:
            yield e

    instance = MagicMock()
    instance.run = _run
    return instance


@pytest.mark.django_db(transaction=True)
async def test_events_captures_merge_request_from_state_snapshot_and_persists_ref():
    # MR carried through state survives encoder serialization as a dict; the
    # capture branch in events() preserves whatever value lands in the snapshot.
    mr = {"source_branch": "feature-y", "merge_request_id": 42}
    snapshot = StateSnapshotEvent(
        type=EventType.STATE_SNAPSHOT,
        raw_event={"metadata": {"langgraph_checkpoint_ns": ""}},
        snapshot={"merge_request": mr},
    )

    persist_calls = []
    release_calls = []

    async def _capture_persist(thread_id, original_ref, captured_mr):
        persist_calls.append((thread_id, original_ref, captured_mr))

    async def _capture_release(thread_id, run_id):
        release_calls.append((thread_id, run_id))

    with (
        patch("chat.api.streaming.open_checkpointer", _mock_ctx),
        patch("chat.api.streaming.set_runtime_ctx", _mock_ctx),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([snapshot])),
        patch("chat.api.streaming.ChatThreadService.persist_ref", side_effect=_capture_persist),
        patch("chat.api.streaming.ChatThreadService.release_run", side_effect=_capture_release),
        patch("chat.api.streaming.ChatThreadService.heartbeat", new=AsyncMock()),
    ):
        streamer = _streamer()
        async for _ in streamer.events():
            pass

    assert persist_calls == [("t-stream", "main", mr)]
    assert release_calls == [("t-stream", "r-1")]


@pytest.mark.django_db(transaction=True)
async def test_events_captures_latest_merge_request_when_multiple_snapshots():
    """Multiple snapshots arrive — last one wins. Regression for
    accidentally rewriting capture as ``last_mr or ...``.
    """
    mr_first = {"source_branch": "feature-x"}
    mr_last = {"source_branch": "feature-final"}
    snap_first = StateSnapshotEvent(
        type=EventType.STATE_SNAPSHOT,
        raw_event={"metadata": {"langgraph_checkpoint_ns": ""}},
        snapshot={"merge_request": mr_first},
    )
    snap_last = StateSnapshotEvent(
        type=EventType.STATE_SNAPSHOT,
        raw_event={"metadata": {"langgraph_checkpoint_ns": ""}},
        snapshot={"merge_request": mr_last},
    )

    persist_calls = []

    async def _capture_persist(thread_id, original_ref, captured_mr):
        persist_calls.append((thread_id, original_ref, captured_mr))

    with (
        patch("chat.api.streaming.open_checkpointer", _mock_ctx),
        patch("chat.api.streaming.set_runtime_ctx", _mock_ctx),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([snap_first, snap_last])),
        patch("chat.api.streaming.ChatThreadService.persist_ref", side_effect=_capture_persist),
        patch("chat.api.streaming.ChatThreadService.release_run", new=AsyncMock()),
        patch("chat.api.streaming.ChatThreadService.heartbeat", new=AsyncMock()),
    ):
        async for _ in _streamer().events():
            pass

    assert persist_calls == [("t-stream", "main", mr_last)]


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
        patch("chat.api.streaming.ChatThreadService.heartbeat", new=AsyncMock()),
    ):
        async for _ in _streamer().events():
            pass

    assert persist_calls == [("t-stream", "main", None)]


@pytest.mark.django_db(transaction=True)
async def test_events_skips_persist_ref_when_run_errored():
    """A partial run must not pin ``ref`` to whatever interim branch a snapshot
    captured before the failure — the user would then reload onto half-built state.
    """
    interim_mr = {"source_branch": "feature-half"}
    snap = StateSnapshotEvent(
        type=EventType.STATE_SNAPSHOT,
        raw_event={"metadata": {"langgraph_checkpoint_ns": ""}},
        snapshot={"merge_request": interim_mr},
    )

    async def _events_then_boom():
        yield snap
        raise RuntimeError("kaboom")

    runner = MagicMock()
    runner.run = lambda _input: _events_then_boom()

    persist_calls: list = []
    release_calls: list = []

    async def _capture_persist(*args):
        persist_calls.append(args)

    async def _capture_release(*args):
        release_calls.append(args)

    with (
        patch("chat.api.streaming.open_checkpointer", _mock_ctx),
        patch("chat.api.streaming.set_runtime_ctx", _mock_ctx),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=runner),
        patch("chat.api.streaming.ChatThreadService.persist_ref", side_effect=_capture_persist),
        patch("chat.api.streaming.ChatThreadService.release_run", side_effect=_capture_release),
        patch("chat.api.streaming.ChatThreadService.heartbeat", new=AsyncMock()),
    ):
        async for _ in _streamer().events():
            pass

    assert persist_calls == []
    # Release still fires regardless of outcome — that's the slot-leak guard.
    assert release_calls == [("t-stream", "r-1")]


@pytest.mark.django_db(transaction=True)
async def test_events_releases_run_even_when_persist_ref_raises():
    # Regression: a DB hiccup in persist_ref must not leave the per-thread
    # slot permanently claimed.
    release_calls = []

    async def _persist_boom(*_a, **_kw):
        raise RuntimeError("db down")

    async def _capture_release(thread_id, run_id):
        release_calls.append((thread_id, run_id))

    with (
        patch("chat.api.streaming.open_checkpointer", _mock_ctx),
        patch("chat.api.streaming.set_runtime_ctx", _mock_ctx),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([])),
        patch("chat.api.streaming.ChatThreadService.persist_ref", side_effect=_persist_boom),
        patch("chat.api.streaming.ChatThreadService.release_run", side_effect=_capture_release),
        patch("chat.api.streaming.ChatThreadService.heartbeat", new=AsyncMock()),
    ):
        async for _ in _streamer().events():
            pass

    assert release_calls == [("t-stream", "r-1")]


def test_streamer_post_init_rejects_thread_id_mismatch():
    """Construction-time guard: thread_id/run_id must match input_data."""
    with pytest.raises(ValueError, match="thread_id mismatch"):
        ChatRunStreamer(
            repo_id="a/b",
            ref="main",
            thread_id="t-foo",
            run_id="r-1",
            input_data=SimpleNamespace(thread_id="t-bar", run_id="r-1"),
            encoder=EventEncoder(accept="text/event-stream"),
        )


def test_streamer_post_init_rejects_run_id_mismatch():
    with pytest.raises(ValueError, match="run_id mismatch"):
        ChatRunStreamer(
            repo_id="a/b",
            ref="main",
            thread_id="t",
            run_id="r-1",
            input_data=SimpleNamespace(thread_id="t", run_id="r-2"),
            encoder=EventEncoder(accept="text/event-stream"),
        )
