"""Direct unit tests for ``ChatRunStreamer.events()``.

Covers the streaming-specific behavior the HTTP-level tests in test_views.py
don't reach — most importantly the STATE_SNAPSHOT-driven ``last_mr`` capture
that keeps the composer MR pill alive across reloads, and the run-slot
lifecycle invariants.

The Run-row lifecycle (``start_chat_run`` / ``finalize_chat_run``) is patched out
here so these tests stay focused on the MR-capture + lock-release invariants; the
Run helpers are covered directly in ``tests/unit_tests/sessions/test_chat_runs.py``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ag_ui.core.events import EventType, StateSnapshotEvent, TextMessageContentEvent

from chat.api.streaming import ChatRunStreamer, RuntimeContextLangGraphAGUIAgent


@pytest.fixture(autouse=True)
def _patch_run_lifecycle():
    """Stub the Run-row helpers so streaming tests don't need a Session row in the DB."""

    async def _fake_start(**_kwargs):
        return SimpleNamespace(pk="run-pk")

    async def _fake_finalize(*_args, **_kwargs):
        return None

    with (
        patch("chat.api.streaming.start_chat_run", side_effect=_fake_start),
        patch("chat.api.streaming.finalize_chat_run", side_effect=_fake_finalize),
        # ``track_usage_metadata`` is a real contextmanager; keep it but with a no-op handler
        # so ``build_usage_summary`` isn't exercised against a live callback here.
        patch("chat.api.streaming.build_usage_summary", return_value=MagicMock(to_dict=lambda: None)),
    ):
        yield


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
    return ChatRunStreamer(repo_id="a/b", ref="main", thread_id="t-stream", run_id="r-1", input_data=input_data)


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
        patch("chat.api.streaming.ChatSessionService.persist_ref", side_effect=_capture_persist),
        patch("chat.api.streaming.SessionLock.release", side_effect=_capture_release),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
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
        patch("chat.api.streaming.ChatSessionService.persist_ref", side_effect=_capture_persist),
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
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
        patch("chat.api.streaming.ChatSessionService.persist_ref", side_effect=_capture_persist),
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
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
        patch("chat.api.streaming.ChatSessionService.persist_ref", side_effect=_capture_persist),
        patch("chat.api.streaming.SessionLock.release", side_effect=_capture_release),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
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
        patch("chat.api.streaming.ChatSessionService.persist_ref", side_effect=_persist_boom),
        patch("chat.api.streaming.SessionLock.release", side_effect=_capture_release),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        async for _ in _streamer().events():
            pass

    assert release_calls == [("t-stream", "r-1")]


@pytest.mark.django_db(transaction=True)
async def test_events_finalizes_failed_when_run_error_event_emitted():
    """ag_ui surfaces an agent failure as a streamed RUN_ERROR event and returns
    normally (no raise) — so the ``async for`` loop completes. The turn must still be
    finalized FAILED, and a failed run must not pin the session ref.

    §F safety guarantee: the upstream RUN_ERROR event's ``.message`` can carry raw
    exception text; it is fine to stream live (yielded to the client) but must never
    be persisted to Run.error_message, which sessions.transcript renders verbatim in
    the transcript on reload. The persisted reason is the same sanitized generic
    constant used by the raised-exception path (parity with
    ``test_events_finalizes_failed_with_generic_message_when_agent_raises``).
    """
    from ag_ui.core.events import RunErrorEvent

    from core.constants import RUN_FAILED_MESSAGE

    err = RunErrorEvent(type=EventType.RUN_ERROR, message="boom in agent", code="run_failed")

    finalize_calls: list = []

    async def _capture_finalize(run_pk, *, success, usage, response_text, error_message=""):
        finalize_calls.append({"success": success, "error_message": error_message})

    persist_calls: list = []

    async def _capture_persist(*args):
        persist_calls.append(args)

    streamed_events: list = []

    with (
        patch("chat.api.streaming.open_checkpointer", _mock_ctx),
        patch("chat.api.streaming.set_runtime_ctx", _mock_ctx),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([err])),
        patch("chat.api.streaming.finalize_chat_run", side_effect=_capture_finalize),
        patch("chat.api.streaming.ChatSessionService.persist_ref", side_effect=_capture_persist),
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        async for event in _streamer().events():
            streamed_events.append(event)

    assert len(finalize_calls) == 1
    assert finalize_calls[0]["success"] is False
    # §F: raw event message must NOT be persisted to Run.error_message.
    assert finalize_calls[0]["error_message"] == RUN_FAILED_MESSAGE
    assert "boom in agent" not in finalize_calls[0]["error_message"]
    # The live client still receives the original upstream message.
    run_error_events = [e for e in streamed_events if getattr(e, "type", None) == EventType.RUN_ERROR]
    assert any(getattr(e, "message", "") == "boom in agent" for e in run_error_events)
    assert persist_calls == []


@pytest.mark.django_db(transaction=True)
async def test_events_finalizes_failed_with_generic_message_when_agent_raises():
    """A raised agent error finalizes the Run FAILED and records a *generic*, user-facing
    reason — never the raw exception class/text (which is logged server-side only). The
    timeline shows a reason instead of a blank FAILED pill, but internal detail can't leak.
    """

    async def _events_then_boom():
        raise RuntimeError("kaboom-secret-internal-detail")
        yield  # pragma: no cover - unreachable, makes this an async generator

    runner = MagicMock()
    runner.run = lambda _input: _events_then_boom()

    finalize_calls: list = []

    async def _capture_finalize(run_pk, *, success, usage, response_text, error_message=""):
        finalize_calls.append({"success": success, "error_message": error_message})

    with (
        patch("chat.api.streaming.open_checkpointer", _mock_ctx),
        patch("chat.api.streaming.set_runtime_ctx", _mock_ctx),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=runner),
        patch("chat.api.streaming.finalize_chat_run", side_effect=_capture_finalize),
        patch("chat.api.streaming.ChatSessionService.persist_ref", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        async for _ in _streamer().events():
            pass

    assert len(finalize_calls) == 1
    assert finalize_calls[0]["success"] is False
    # The persisted reason must be the generic user-facing message, not the raw
    # exception class/text — that detail belongs in the server logs only.
    assert finalize_calls[0]["error_message"] == "Run failed. Check server logs for details."
    assert "RuntimeError" not in finalize_calls[0]["error_message"]
    assert "kaboom-secret-internal-detail" not in finalize_calls[0]["error_message"]


class _FakeGraph:
    """Minimal stand-in for a CompiledStateGraph. ``nodes`` feeds the subgraph
    scan in ``LangGraphAGUIAgent.__init__``; ``astream_events`` exists only so
    upstream's signature probe sees a ``context`` parameter (newer LangGraph),
    which makes ``get_stream_kwargs`` populate ``context`` from configurable.
    """

    nodes: dict = {}

    async def astream_events(self, _input=None, *, context=None, **kwargs):  # pragma: no cover - never invoked
        yield None


def test_get_stream_kwargs_overrides_configurable_context_with_runtime_ctx():
    """Regression: newer LangGraph's ``astream_events`` accepts ``context``, so
    upstream builds ``context={"thread_id": ...}`` from ``config['configurable']``.
    Our override must replace that dict with the ``RuntimeCtx`` instance — passing
    the dict through makes LangGraph's ``_coerce_context`` call ``RuntimeCtx(**ctx)``,
    raising ``TypeError: RuntimeCtx.__init__() got an unexpected keyword argument 'thread_id'``.
    """
    runtime_ctx = object()  # sentinel; identity is all we assert on
    agent = RuntimeContextLangGraphAGUIAgent(
        name="DAIV", description="d", graph=_FakeGraph(), config={}, runtime_context=runtime_ctx
    )

    kwargs = agent.get_stream_kwargs(
        input={}, config={"configurable": {"thread_id": "abc"}}, subgraphs=False, version="v2"
    )

    assert kwargs["context"] is runtime_ctx


def test_streamer_post_init_rejects_thread_id_mismatch():
    """Construction-time guard: thread_id/run_id must match input_data."""
    with pytest.raises(ValueError, match="thread_id mismatch"):
        ChatRunStreamer(
            repo_id="a/b",
            ref="main",
            thread_id="t-foo",
            run_id="r-1",
            input_data=SimpleNamespace(thread_id="t-bar", run_id="r-1"),
        )


def test_streamer_post_init_rejects_run_id_mismatch():
    with pytest.raises(ValueError, match="run_id mismatch"):
        ChatRunStreamer(
            repo_id="a/b",
            ref="main",
            thread_id="t",
            run_id="r-1",
            input_data=SimpleNamespace(thread_id="t", run_id="r-2"),
        )


@pytest.mark.django_db(transaction=True)
async def test_events_stops_with_run_cancelled_when_cancel_flag_set():
    """The cancel endpoint sets a Redis flag; the streamer polls it at heartbeat
    cadence and must stop the run, surface a RUN_ERROR(code=run_cancelled) so
    stream observers see why, and finalize the Run FAILED with the user-facing
    stop message.
    """
    snap = StateSnapshotEvent(
        type=EventType.STATE_SNAPSHOT,
        raw_event={"metadata": {"langgraph_checkpoint_ns": ""}},
        snapshot={"messages": []},
    )

    finalize_calls: list = []

    async def _capture_finalize(run_pk, *, success, usage, response_text, error_message=""):
        finalize_calls.append({"success": success, "error_message": error_message})

    release_calls: list = []

    async def _capture_release(*args):
        release_calls.append(args)

    with (
        patch("chat.api.streaming.open_checkpointer", _mock_ctx),
        patch("chat.api.streaming.set_runtime_ctx", _mock_ctx),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        # Two events queued, but the cancel check (interval patched to 0) fires
        # after the first — the second must never be yielded.
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([snap, snap])),
        patch("chat.api.streaming.HEARTBEAT_INTERVAL_S", 0.0),
        patch("chat.api.relay.RunRelay.cancel_requested", new=AsyncMock(return_value=True)),
        patch("chat.api.streaming.finalize_chat_run", side_effect=_capture_finalize),
        patch("chat.api.streaming.ChatSessionService.persist_ref", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.release", side_effect=_capture_release),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        seen = [e async for e in _streamer().events()]

    assert seen[0].type == EventType.STATE_SNAPSHOT
    assert seen[-1].type == EventType.RUN_ERROR
    assert seen[-1].code == "run_cancelled"
    assert len(seen) == 2  # second snapshot suppressed by the break
    assert finalize_calls == [{"success": False, "error_message": "Stopped by user."}]
    assert release_calls == [("t-stream", "r-1")]


@pytest.mark.django_db(transaction=True)
async def test_events_finalizes_interrupted_on_task_cancellation():
    """A hard task cancel (local stop, or process shutdown) must finalize the Run
    FAILED with the interrupted message rather than a blank one, then re-raise.
    """
    import asyncio

    started = asyncio.Event()

    async def _hang(_input):
        yield StateSnapshotEvent(
            type=EventType.STATE_SNAPSHOT,
            raw_event={"metadata": {"langgraph_checkpoint_ns": ""}},
            snapshot={"messages": []},
        )
        started.set()
        await asyncio.Event().wait()  # hang until cancelled

    runner_mock = MagicMock()
    runner_mock.run = _hang

    finalize_calls: list = []

    async def _capture_finalize(run_pk, *, success, usage, response_text, error_message=""):
        finalize_calls.append({"success": success, "error_message": error_message})

    with (
        patch("chat.api.streaming.open_checkpointer", _mock_ctx),
        patch("chat.api.streaming.set_runtime_ctx", _mock_ctx),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=runner_mock),
        patch("chat.api.streaming.finalize_chat_run", side_effect=_capture_finalize),
        patch("chat.api.streaming.ChatSessionService.persist_ref", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):

        async def _drain():
            async for _ in _streamer().events():
                pass

        task = asyncio.create_task(_drain())
        await started.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert finalize_calls == [{"success": False, "error_message": "Run was interrupted before completing."}]


@pytest.mark.django_db(transaction=True)
async def test_events_stops_when_slot_lost_to_stale_takeover():
    """A stale takeover reassigns the run slot while we're still streaming.
    ``SessionLock.heartbeat`` then returns False; the streamer must stop writing
    to a checkpoint it no longer owns — surface a RUN_ERROR(code=run_interrupted),
    finalize the Run FAILED with the interrupted message, and not yield further
    agent events.
    """
    snap = StateSnapshotEvent(
        type=EventType.STATE_SNAPSHOT,
        raw_event={"metadata": {"langgraph_checkpoint_ns": ""}},
        snapshot={"messages": []},
    )

    finalize_calls: list = []

    async def _capture_finalize(run_pk, *, success, usage, response_text, error_message=""):
        finalize_calls.append({"success": success, "error_message": error_message})

    with (
        patch("chat.api.streaming.open_checkpointer", _mock_ctx),
        patch("chat.api.streaming.set_runtime_ctx", _mock_ctx),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        # Two events queued; the heartbeat check (interval patched to 0) fires after
        # the first and reports the slot lost — the second must never be yielded.
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([snap, snap])),
        patch("chat.api.streaming.HEARTBEAT_INTERVAL_S", 0.0),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock(return_value=False)),
        # Cancel flag never set: the stop is driven purely by the lost slot.
        patch("chat.api.relay.RunRelay.cancel_requested", new=AsyncMock(return_value=False)),
        patch("chat.api.streaming.finalize_chat_run", side_effect=_capture_finalize),
        patch("chat.api.streaming.ChatSessionService.persist_ref", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
    ):
        seen = [e async for e in _streamer().events()]

    assert seen[0].type == EventType.STATE_SNAPSHOT
    assert seen[-1].type == EventType.RUN_ERROR
    assert seen[-1].code == "run_interrupted"
    assert len(seen) == 2  # second snapshot suppressed by the break
    assert finalize_calls == [{"success": False, "error_message": "Run was interrupted before completing."}]


class TestStartChatRunPersistence:
    """Isolated class so the module-level autouse ``_patch_run_lifecycle`` can be
    overridden here — the real ``start_chat_run`` must run against the DB.
    """

    @pytest.fixture(autouse=True)
    def _patch_run_lifecycle(self):
        """Override the module-level autouse: do NOT stub start_chat_run so the real
        DB-backed implementation runs for the persistence assertion.
        """

    @pytest.mark.django_db(transaction=True)
    async def test_start_chat_run_persists_message_id(self):
        import uuid

        from sessions.models import Session, SessionOrigin

        from chat.api.streaming import start_chat_run

        session = await Session.objects.acreate(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/project", ref="main"
        )
        run = await start_chat_run(
            session_id=session.thread_id,
            user_id=None,
            prompt="hello",
            repo_id="group/project",
            ref="main",
            message_id="h-99",
        )
        assert run.message_id == "h-99"


@pytest.mark.django_db(transaction=True)
async def test_events_buffers_text_deltas_into_result_summary():
    """Assistant text deltas are buffered (capped at 2000 chars) and handed to
    ``finalize_chat_run`` as ``response_text`` — this feeds the persisted,
    user-visible run result summary.
    """

    def _text(delta: str) -> TextMessageContentEvent:
        return TextMessageContentEvent(type=EventType.TEXT_MESSAGE_CONTENT, message_id="m1", delta=delta)

    # First a small delta, then one that overflows the 2000-char cap.
    events = [_text("Hello "), _text("x" * 2500)]

    captured: dict = {}

    async def _capture_finalize(run_pk, *, success, usage, response_text, error_message=""):
        captured["response_text"] = response_text
        captured["success"] = success

    with (
        patch("chat.api.streaming.open_checkpointer", _mock_ctx),
        patch("chat.api.streaming.set_runtime_ctx", _mock_ctx),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent(events)),
        patch("chat.api.streaming.finalize_chat_run", side_effect=_capture_finalize),
        patch("chat.api.streaming.ChatSessionService.persist_ref", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        async for _ in _streamer().events():
            pass

    assert captured["success"] is True
    assert captured["response_text"].startswith("Hello ")
    assert len(captured["response_text"]) == 2000  # truncated at the cap
