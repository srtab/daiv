"""Direct unit tests for ``ChatRunStreamer.events()``.

Covers what the HTTP-level tests in test_views.py don't reach: the turn's lifecycle on top of the
executor's ``stream_run`` (the ref sync and the merge request claim read the finished turn's checkpoint), the
run-slot invariants, and the AG-UI adapter's event shaping.

The Run-row lifecycle (``start_chat_run`` / ``finalize_chat_run``) is patched out
here so these tests stay focused on the stream and slot invariants; the
Run helpers are covered directly in ``tests/unit_tests/sessions/test_chat_runs.py``.
"""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ag_ui.core import RunAgentInput
from ag_ui.core.events import EventType, StateSnapshotEvent, TextMessageContentEvent
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel, GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from sessions import artifacts
from sessions.executor.result import parse_agent_result
from sessions.live.relay import RunRelay
from sessions.models import Run, Session

from automation.agent.agent_settings import resolve_agent_settings
from automation.agent.questions import render_questions
from automation.agent.references import ExternalRef
from chat.api import runner
from chat.api.streaming import ChatRunStreamer
from core.constants import RUN_FAILED_MESSAGE
from core.site_settings import site_settings
from tests.unit_tests.conftest import SAMPLE_QUESTION_PAYLOAD, ask_user_question_messages, site_snapshot
from tests.unit_tests.sessions.conftest import amake_job_session, claim_recorder
from tests.unit_tests.sessions.executor.conftest import agent_stack


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
        patch("sessions.executor.run._record_measurements", new=AsyncMock()),
        # Chat's own usage summary; the executor's ``_after_run`` still builds one from the live handler.
        patch("chat.api.streaming.build_usage_summary", return_value=MagicMock(to_dict=lambda: None)),
    ):
        yield


def _mock_ctx(*_args, **_kwargs):
    """Async context manager yielding a MagicMock — stands in for ``open_checkpointer``
    / ``set_runtime_ctx`` so we don't touch Redis or clone a repo. ``repo.ref`` matches
    ``_streamer``'s ref so the ref-fallback branch stays dormant here.
    """
    ctx = MagicMock()
    entered = MagicMock(sandbox=None, sandbox_client=None)
    entered.repo.ref = "main"
    entered.repo.head_detached = False
    ctx.__aenter__ = AsyncMock(return_value=entered)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _graph(**values) -> MagicMock:
    """The compiled agent: ``stream_run`` reads the finished turn's checkpoint, ``values``, off it."""
    return MagicMock(aget_state=AsyncMock(return_value=SimpleNamespace(values=values)))


@pytest.fixture(autouse=True)
def _executor_stack():
    """Stub what ``stream_run`` builds around a turn: a clone on ``main`` and an agent whose checkpoint is empty."""
    with agent_stack(_graph()) as stack:
        yield stack


def _run_input(*messages: dict) -> RunAgentInput:
    return RunAgentInput(
        thread_id="t-stream", run_id="r-1", state={}, messages=list(messages), tools=[], context=[], forwarded_props={}
    )


def _streamer(input_data=None, **kwargs) -> ChatRunStreamer:
    if input_data is None:
        input_data = _run_input()
    return ChatRunStreamer(
        repo_id="a/b", ref="main", thread_id="t-stream", run_id="r-1", input_data=input_data, **kwargs
    )


def test_the_run_spec_carries_the_prompt_as_the_turns_input():
    """The stream sends the prompt itself; the executor reads it to skip the sandbox for a builtin slash command."""
    assert _streamer(prompt="/help")._run_spec().input_messages == (HumanMessage(content="/help"),)


def test_a_blank_ref_runs_on_the_default_branch():
    streamer = ChatRunStreamer(repo_id="a/b", ref="", thread_id="t-stream", run_id="r-1", input_data=_run_input())
    assert streamer._run_spec().ref is None


def _mock_agent(events):
    """Patch ``RuntimeContextLangGraphAGUIAgent`` so its instance's ``run()`` yields
    the supplied iterable of AGUI events.
    """

    async def _run(_input):
        for e in events:
            yield e

    instance = MagicMock(graph_stream=None)
    instance.run = _run
    return instance


def _snapshot(**values) -> StateSnapshotEvent:
    return StateSnapshotEvent(
        type=EventType.STATE_SNAPSHOT, raw_event={"metadata": {"langgraph_checkpoint_ns": ""}}, snapshot=values
    )


@pytest.mark.django_db(transaction=True)
async def test_events_persists_the_checkpoints_merge_request_and_releases_the_slot():
    mr = {"source_branch": "feature-y", "merge_request_id": 42}
    persist_calls = []
    release_calls = []

    async def _capture_persist(*, thread_id, current_ref, merge_request, published):
        persist_calls.append((thread_id, current_ref, merge_request))

    async def _capture_release(thread_id, run_id):
        release_calls.append((thread_id, run_id))

    with (
        patch("automation.agent.graph.create_daiv_agent", new=AsyncMock(return_value=_graph(merge_request=mr))),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([_snapshot()])),
        patch("sessions.executor.run.apersist_session_ref", side_effect=_capture_persist),
        patch("chat.api.streaming.SessionLock.release", side_effect=_capture_release),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        async for _ in _streamer().events():
            pass

    assert persist_calls == [("t-stream", "main", mr)]
    assert release_calls == [("t-stream", "r-1")]


@pytest.mark.django_db(transaction=True)
async def test_events_persists_the_checkpoints_merge_request_over_the_streamed_one():
    """The streamed snapshots feed the composer's MR pill; the ref sync reads the finished turn's checkpoint."""
    streamed = {"source_branch": "feature-x"}
    finished = {"source_branch": "feature-final"}
    persist_calls = []

    async def _capture_persist(*, thread_id, current_ref, merge_request, published):
        persist_calls.append((thread_id, current_ref, merge_request))

    with (
        patch("automation.agent.graph.create_daiv_agent", new=AsyncMock(return_value=_graph(merge_request=finished))),
        patch(
            "chat.api.streaming.RuntimeContextLangGraphAGUIAgent",
            return_value=_mock_agent([_snapshot(merge_request=streamed)]),
        ),
        patch("sessions.executor.run.apersist_session_ref", side_effect=_capture_persist),
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        async for _ in _streamer().events():
            pass

    assert persist_calls == [("t-stream", "main", finished)]


@pytest.mark.django_db(transaction=True)
async def test_events_persists_none_when_the_checkpoint_has_no_merge_request():
    # A turn that ends without a merge request still reaches apersist_session_ref, with None.
    persist_calls = []

    async def _capture_persist(*, thread_id, current_ref, merge_request, published):
        persist_calls.append((thread_id, current_ref, merge_request))

    with (
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([_snapshot()])),
        patch("sessions.executor.run.apersist_session_ref", side_effect=_capture_persist),
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

    runner = MagicMock(graph_stream=None)
    runner.run = lambda _input: _events_then_boom()

    persist_calls: list = []
    release_calls: list = []

    async def _capture_persist(*, thread_id, current_ref, merge_request, published):
        persist_calls.append((thread_id, current_ref, merge_request))

    async def _capture_release(*args):
        release_calls.append(args)

    with (
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=runner),
        patch("sessions.executor.run.apersist_session_ref", side_effect=_capture_persist),
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
    # Regression: a DB hiccup in the ref sync must not leave the per-thread
    # slot permanently claimed.
    release_calls = []

    async def _persist_boom(*_a, **_kw):
        raise RuntimeError("db down")

    async def _capture_release(thread_id, run_id):
        release_calls.append((thread_id, run_id))

    with (
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([])),
        patch("sessions.executor.run.apersist_session_ref", side_effect=_persist_boom),
        patch("chat.api.streaming.SessionLock.release", side_effect=_capture_release),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        async for _ in _streamer().events():
            pass

    assert release_calls == [("t-stream", "r-1")]


@pytest.mark.django_db(transaction=True)
async def test_events_streams_the_agent_with_the_chat_run_bound_for_artifacts():
    bound = []

    async def _run(_input):
        bound.append(artifacts._active_run_id.get())
        yield _snapshot()

    with (
        patch(
            "chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=MagicMock(run=_run, graph_stream=None)
        ),
        patch("sessions.executor.run.apersist_session_ref", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        async for _ in _streamer().events():
            pass

    assert bound == ["run-pk"]
    assert artifacts._active_run_id.get() is None


@pytest.mark.django_db(transaction=True)
async def test_a_turn_hands_the_graph_only_its_new_message_whatever_the_client_echoes(_executor_stack):
    seen = []

    class Probe(AgentMiddleware):
        async def abefore_model(self, state, runtime):
            seen.append([m.id for m in state["messages"]])

    agent = create_agent(
        model=FakeMessagesListChatModel(responses=[AIMessage(content="ok", id="a2")]),
        tools=[],
        middleware=[Probe()],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "t-stream"}}
    earlier = [HumanMessage(content="first turn", id="h1"), AIMessage(content="first reply", id="a1")]
    await agent.aupdate_state(config, {"messages": earlier})
    _executor_stack.create_agent.return_value = agent
    _executor_stack.langsmith.return_value = config
    input_data = _run_input(
        {"id": "p0", "role": "user", "content": "before a clear"},
        {"id": "h1", "role": "user", "content": "first turn"},
        {"id": "h2", "role": "user", "content": "second turn"},
    )

    with (
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        async for _ in _streamer(input_data).events():
            pass

    assert seen == [["h1", "a1", "h2"]]


@pytest.mark.django_db(transaction=True)
async def test_events_finalizes_failed_when_run_error_event_emitted():
    """ag_ui surfaces an agent failure as a streamed RUN_ERROR event and returns
    normally (no raise) — so the ``async for`` loop completes. The turn must still be
    finalized FAILED, and a failed run must not pin the session ref.

    §F safety guarantee: the upstream RUN_ERROR event's ``.message`` can carry raw
    exception text; it is neither streamed nor persisted to Run.error_message, which
    sessions.transcript renders verbatim in the transcript on reload. Both carry the
    same sanitized generic constant as the raised-exception path (parity with
    ``test_events_finalizes_failed_with_generic_message_when_agent_raises``).
    """
    from ag_ui.core.events import RunErrorEvent

    from core.constants import RUN_FAILED_MESSAGE

    err = RunErrorEvent(type=EventType.RUN_ERROR, message="boom in agent", code="run_failed")

    finalize_calls: list = []

    async def _capture_finalize(_run, *, success, error_message="", **_kwargs):
        finalize_calls.append({"success": success, "error_message": error_message})

    persist_calls: list = []

    async def _capture_persist(*, thread_id, current_ref, merge_request, published):
        persist_calls.append((thread_id, current_ref, merge_request))

    streamed_events: list = []

    with (
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([err])),
        patch("chat.api.streaming.finalize_chat_run", side_effect=_capture_finalize),
        patch("sessions.executor.run.apersist_session_ref", side_effect=_capture_persist),
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
    run_error_events = [e for e in streamed_events if getattr(e, "type", None) == EventType.RUN_ERROR]
    assert [(e.message, e.code) for e in run_error_events] == [(RUN_FAILED_MESSAGE, "run_failed")]
    assert persist_calls == []


@pytest.mark.django_db(transaction=True)
async def test_a_turn_closes_the_graph_run_ag_ui_abandoned():
    from ag_ui.core.events import RunErrorEvent

    closed: list[str] = []

    async def _graph_stream():
        try:
            yield {}
            await asyncio.sleep(10)
        finally:
            closed.append("graph")

    graph_stream = _graph_stream()
    await anext(graph_stream)
    agent = _mock_agent([RunErrorEvent(type=EventType.RUN_ERROR, message="adapter bug")])
    agent.graph_stream = graph_stream

    with (
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=agent),
        patch("chat.api.streaming.finalize_chat_run", new=AsyncMock()),
        patch("sessions.executor.run.apersist_session_ref", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        async for _ in _streamer().events():
            pass

    assert closed == ["graph"]


@pytest.mark.django_db(transaction=True)
async def test_events_finalizes_failed_with_generic_message_when_agent_raises():
    """A raised agent error finalizes the Run FAILED and records a *generic*, user-facing
    reason — never the raw exception class/text (which is logged server-side only). The
    timeline shows a reason instead of a blank FAILED pill, but internal detail can't leak.
    """

    async def _events_then_boom():
        raise RuntimeError("kaboom-secret-internal-detail")
        yield  # pragma: no cover - unreachable, makes this an async generator

    runner = MagicMock(graph_stream=None)
    runner.run = lambda _input: _events_then_boom()

    finalize_calls: list = []

    async def _capture_finalize(_run, *, success, error_message="", **_kwargs):
        finalize_calls.append({"success": success, "error_message": error_message})

    with (
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=runner),
        patch("chat.api.streaming.finalize_chat_run", side_effect=_capture_finalize),
        patch("sessions.executor.run.apersist_session_ref", new=AsyncMock()),
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


@pytest.mark.django_db(transaction=True)
async def test_an_exception_ag_ui_swallows_never_reaches_the_relay(_executor_stack, fake_redis):
    """ag-ui turns a graph exception into a RUN_ERROR carrying its text, and repo readers can stream the run."""

    class FailingModel(GenericFakeChatModel):
        async def _astream(self, *args, **kwargs):
            raise RuntimeError("secret /srv/internal")
            yield

        async def _agenerate(self, *args, **kwargs):
            raise RuntimeError("secret /srv/internal")

    _executor_stack.create_agent.return_value = create_agent(
        model=FailingModel(messages=iter([])), tools=[], checkpointer=InMemorySaver()
    )

    with (
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        await runner.run_to_relay(_streamer())

    published = [fields.get("data", "") for _id, fields in fake_redis.streams[RunRelay("t-stream", "r-1").events_key]]
    errors = [json.loads(data) for data in published if '"RUN_ERROR"' in data]
    assert [(e["message"], e["code"]) for e in errors] == [(RUN_FAILED_MESSAGE, "run_failed")]
    assert not any("secret" in data for data in published)


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
    """The cancel endpoint sets a Redis flag; the executor polls it at heartbeat
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

    async def _capture_finalize(_run, *, success, error_message="", **_kwargs):
        finalize_calls.append({"success": success, "error_message": error_message})

    release_calls: list = []

    async def _capture_release(*args):
        release_calls.append(args)

    with (
        # Two events queued, but the cancel check (interval patched to 0) fires
        # after the first — the second must never be yielded.
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([snap, snap])),
        patch("sessions.executor.run.STREAM_HEARTBEAT_INTERVAL_S", 0.0),
        patch("sessions.live.relay.RunRelay.cancel_requested", new=AsyncMock(return_value=True)),
        patch("chat.api.streaming.finalize_chat_run", side_effect=_capture_finalize),
        patch("sessions.executor.run.apersist_session_ref", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.release", side_effect=_capture_release),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        seen = [e async for e in _streamer().events()]

    assert [e.type for e in seen] == [EventType.CUSTOM, EventType.STATE_SNAPSHOT, EventType.RUN_ERROR]
    assert seen[-1].code == "run_cancelled"  # the stop closed the stream before the second snapshot
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

    async def _capture_finalize(_run, *, success, error_message="", **_kwargs):
        finalize_calls.append({"success": success, "error_message": error_message})

    with (
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=runner_mock),
        patch("chat.api.streaming.finalize_chat_run", side_effect=_capture_finalize),
        patch("sessions.executor.run.apersist_session_ref", new=AsyncMock()),
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

    async def _capture_finalize(_run, *, success, error_message="", **_kwargs):
        finalize_calls.append({"success": success, "error_message": error_message})

    with (
        # Two events queued; the heartbeat check (interval patched to 0) fires after
        # the first and reports the slot lost — the second must never be yielded.
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([snap, snap])),
        patch("sessions.executor.run.STREAM_HEARTBEAT_INTERVAL_S", 0.0),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock(return_value=False)),
        # Cancel flag never set: the stop is driven purely by the lost slot.
        patch("sessions.live.relay.RunRelay.cancel_requested", new=AsyncMock(return_value=False)),
        patch("chat.api.streaming.finalize_chat_run", side_effect=_capture_finalize),
        patch("sessions.executor.run.apersist_session_ref", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
    ):
        seen = [e async for e in _streamer().events()]

    assert [e.type for e in seen] == [EventType.CUSTOM, EventType.STATE_SNAPSHOT, EventType.RUN_ERROR]
    assert seen[-1].code == "run_interrupted"  # the stop closed the stream before the second snapshot
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

    async def _capture_finalize(_run, *, success, response_text, **_kwargs):
        captured["response_text"] = response_text
        captured["success"] = success

    with (
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent(events)),
        patch("chat.api.streaming.finalize_chat_run", side_effect=_capture_finalize),
        patch("sessions.executor.run.apersist_session_ref", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        async for _ in _streamer().events():
            pass

    assert captured["success"] is True
    assert captured["response_text"].startswith("Hello ")
    assert len(captured["response_text"]) == 2000  # truncated at the cap


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("checkpoint_messages", "expected"),
    [
        pytest.param(
            [HumanMessage(content="migrate"), *ask_user_question_messages()],
            {"question": SAMPLE_QUESTION_PAYLOAD, "response_text": render_questions(SAMPLE_QUESTION_PAYLOAD)},
            id="question",
        ),
        pytest.param([AIMessage(content="done")], {"question": None, "response_text": ""}, id="no-question"),
    ],
)
async def test_the_turn_finalizes_with_the_question_it_ended_on(checkpoint_messages, expected):
    """The question is never streamed as text; the checkpoint is the only place the finished turn reads it from."""
    finalize_calls: list = []

    async def _capture_finalize(_run, *, response_text, question=None, **_kwargs):
        finalize_calls.append({"question": question, "response_text": response_text})

    with (
        agent_stack(_graph(messages=checkpoint_messages)),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([])),
        patch("chat.api.streaming.finalize_chat_run", side_effect=_capture_finalize),
        patch("sessions.executor.run.apersist_session_ref", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        async for _ in _streamer().events():
            pass

    assert finalize_calls == [expected]


@pytest.mark.django_db(transaction=True)
async def test_the_turn_finalizes_with_its_agent_result(_executor_stack):
    finalize_calls: list = []

    async def _capture_finalize(_run, *, agent_result=None, **_kwargs):
        finalize_calls.append(agent_result)

    agent_result = parse_agent_result({"merge_request_id": 7, "merge_request_web_url": "https://git.example/mr/7"})
    _executor_stack.build_result.side_effect = None
    _executor_stack.build_result.return_value = agent_result
    with (
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([])),
        patch("chat.api.streaming.finalize_chat_run", side_effect=_capture_finalize),
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        async for _ in _streamer().events():
            pass

    assert finalize_calls == [agent_result]


@pytest.mark.django_db(transaction=True)
async def test_events_falls_back_and_self_heals_ref_when_branch_gone():
    """When set_runtime_ctx checked out a different ref than requested (fallback), the streamer
    self-heals Session.ref and emits a ref_fallback event before agent output."""

    def _fallback_ctx(*_args, **_kwargs):
        ctx = MagicMock()
        entered = MagicMock(sandbox=None, sandbox_client=None)
        entered.repo.ref = "dev"  # differs from requested "main" → fallback happened
        ctx.__aenter__ = AsyncMock(return_value=entered)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    reset_calls = []

    async def _capture_reset(*, thread_id, new_ref):
        reset_calls.append((thread_id, new_ref))

    emitted = []

    start_chat_run = AsyncMock(return_value=SimpleNamespace(pk="run-pk"))

    with (
        patch("automation.agent.context.set_runtime_ctx", _fallback_ctx),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([])),
        patch("chat.api.streaming.start_chat_run", new=start_chat_run),
        patch("sessions.executor.run.apersist_session_ref", new=AsyncMock()),
        patch("sessions.executor.run.areset_session_ref", side_effect=_capture_reset),
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        streamer = _streamer()  # ref="main"
        async for ev in streamer.events():
            emitted.append(ev)

    assert reset_calls == [("t-stream", "dev")]
    fallback_events = [e for e in emitted if e.type == EventType.CUSTOM and getattr(e, "name", None) == "ref_fallback"]
    assert len(fallback_events) == 1
    assert fallback_events[0].value == {"requested": "main", "using": "dev"}
    # The Run row must record the effective (fallen-back) ref, not the requested one.
    assert start_chat_run.call_args.kwargs["ref"] == "dev"


@pytest.mark.django_db(transaction=True)
async def test_events_hands_the_turns_settings_to_the_executor(_executor_stack):
    """A setting ``_run_spec`` drops or renames is swallowed silently: the turn runs on defaults (every footer
    vanishes without ``references``, the pinned model without ``agent_model``)."""
    refs = (ExternalRef(key="RT-77", provider="rt", url="https://rt.example.com/77"),)
    captured = {}

    def _capture_ctx(*_args, **kwargs):
        captured.update(kwargs)
        return _mock_ctx()

    langsmith = MagicMock(return_value={})
    with (
        patch("automation.agent.context.set_runtime_ctx", _capture_ctx),
        patch("automation.agent.utils.build_langsmith_config", langsmith),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([])),
        patch("sessions.executor.run.apersist_session_ref", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        streamer = ChatRunStreamer(
            repo_id="a/b",
            ref="main",
            thread_id="t-stream",
            run_id="r-1",
            input_data=_run_input(),
            user_id=7,
            sandbox_environment_id="env-1",
            agent_model="openrouter:z-ai/glm-5.2",
            agent_thinking_level="low",
            mcp_overrides={"sentry": "off"},
            external_refs=refs,
        )
        async for _ in streamer.events():
            pass

    _executor_stack.build_spec.assert_awaited_once_with("env-1")
    assert {key: captured[key] for key in ("sandbox_spec", "acting_user_id", "references")} == {
        "sandbox_spec": _executor_stack.build_spec.return_value,
        "acting_user_id": 7,
        "references": refs,
    }
    _executor_stack.build_servers.assert_called_once_with(7, {"sentry": "off"})
    run = _executor_stack.resolve.call_args.kwargs["run"]
    assert (run.agent_model, run.agent_thinking_level) == ("openrouter:z-ai/glm-5.2", "low")
    assert (langsmith.call_args.kwargs["trigger"], langsmith.call_args.kwargs["extra_metadata"]) == (
        "chat",
        {"override_source": "explicit"},
    )


@pytest.mark.django_db(transaction=True)
async def test_chat_runs_on_the_resolved_recursion_limit(_executor_stack):
    _executor_stack.resolve.side_effect = resolve_agent_settings
    with (
        patch.object(site_settings, "snapshot", return_value=site_snapshot(agent_recursion_limit=123)),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([])) as agui,
        patch("sessions.executor.run.apersist_session_ref", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
        patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
    ):
        async for _ in _streamer().events():
            pass

    assert _executor_stack.create_agent.await_args.kwargs["settings"].recursion_limit == 123
    assert agui.call_args.kwargs["config"]["recursion_limit"] == 123


class TestResolvedModelRecord:
    """Isolated class so the module-level autouse ``_patch_run_lifecycle`` can be overridden here — the real
    ``start_chat_run`` creates the row the executor's model record would have to land on."""

    @pytest.fixture(autouse=True)
    def _patch_run_lifecycle(self):
        """Override the module-level autouse: the real ``start_chat_run`` creates the row."""

    @pytest.mark.django_db(transaction=True)
    async def test_a_chat_run_records_the_model_it_ran_on_but_leaves_the_session_unpinned(self):
        await amake_job_session("t-stream")

        with (
            patch("chat.api.streaming.finalize_chat_run", new=AsyncMock()),
            patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([])),
            patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
            patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
        ):
            async for _ in _streamer().events():
                pass

        run = await Run.objects.aget(session_id="t-stream")
        session = await Session.objects.aget(thread_id="t-stream")
        assert (run.agent_model, run.agent_thinking_level) == ("claude-4-7-opus", "medium")
        assert (session.agent_model, session.agent_thinking_level) == ("", "")


def _failing_ctx(*_args, **_kwargs):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("clone failed"))
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


@contextmanager
def _recorded_turn(
    agui_agent,
    *,
    heartbeat: bool | AsyncMock = True,
    cancel: bool = False,
    runtime_ctx=_mock_ctx,
    graph=None,
    checkpoint: dict | None = None,
    errors: dict[str, Exception] | None = None,
):
    """Patch the chat turn's collaborators around ``agui_agent``; yield ``calls``, which logs start and each
    after-run step in order. ``checkpoint`` is the finished turn's state, which the ref sync and the claim read.
    ``errors`` maps a step name to the exception it raises after it is logged."""
    calls: list[tuple] = []
    errors = errors or {}

    def _log(step: str, *args) -> None:
        calls.append((step, *args))
        if step in errors:
            raise errors[step]

    async def _start(**_kwargs):
        _log("start")
        return SimpleNamespace(pk="run-pk")

    async def _finalize(_run, *, success, error_message="", **_kwargs):
        _log("finalize", success, error_message)

    async def _persist(*, thread_id, current_ref, merge_request, published):
        _log("persist", current_ref, merge_request)

    record_claim = claim_recorder([])

    async def _claim(**kwargs):
        _log("claim", kwargs["run_id"], kwargs["merge_request"])
        await record_claim(**kwargs)

    async def _release(thread_id, run_id):
        _log("release", thread_id, run_id)

    heartbeat_mock = heartbeat if isinstance(heartbeat, AsyncMock) else AsyncMock(return_value=heartbeat)
    compiled = graph or _graph(**(checkpoint or {}))
    with (
        patch("automation.agent.context.set_runtime_ctx", runtime_ctx),
        patch("automation.agent.graph.create_daiv_agent", new=AsyncMock(return_value=compiled)),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=agui_agent),
        patch("chat.api.streaming.start_chat_run", side_effect=_start),
        patch("chat.api.streaming.finalize_chat_run", side_effect=_finalize),
        patch("sessions.executor.run.apersist_session_ref", side_effect=_persist),
        patch("sessions.executor.run.aclaim_ownership", _claim),
        patch("chat.api.streaming.SessionLock.release", side_effect=_release),
        patch("chat.api.streaming.SessionLock.heartbeat", new=heartbeat_mock),
        patch("sessions.executor.run.STREAM_HEARTBEAT_INTERVAL_S", 0.0),
        patch("sessions.live.relay.RunRelay.cancel_requested", new=AsyncMock(return_value=cancel)),
    ):
        yield calls


def _raising_agent(*events):
    async def _run(_input):
        for event in events:
            yield event
        raise RuntimeError("kaboom")

    agent = MagicMock()
    agent.run = _run
    return agent


_RELEASE = ("release", "t-stream", "r-1")


class TestChatAfterRunMatrix:
    async def test_a_clean_turn_runs_the_after_run_steps_in_order(self):
        mr = {"source_branch": "feature-y", "merge_request_id": 42}

        with _recorded_turn(
            _mock_agent([_snapshot(merge_request=mr, published=True)]),
            checkpoint={"merge_request": mr, "published": True},
        ) as calls:
            [event async for event in _streamer().events()]

        assert calls == [("start",), ("persist", "main", mr), ("claim", "run-pk", mr), ("finalize", True, ""), _RELEASE]

    async def test_a_raised_agent_error_becomes_a_run_error_event(self):
        from core.constants import RUN_FAILED_MESSAGE

        graph = MagicMock(aget_state=AsyncMock(), aupdate_state=AsyncMock())

        with _recorded_turn(_raising_agent(_snapshot(merge_request=None)), graph=graph) as calls:
            events = [event async for event in _streamer().events()]

        assert events[-1].type == EventType.RUN_ERROR
        assert (events[-1].code, events[-1].message) == ("run_failed", RUN_FAILED_MESSAGE)
        assert calls == [("start",), ("finalize", False, RUN_FAILED_MESSAGE), _RELEASE]
        graph.aget_state.assert_not_awaited()
        graph.aupdate_state.assert_not_awaited()

    async def test_an_emitted_run_error_fails_the_turn_and_skips_ref_and_watch(self):
        from ag_ui.core.events import RunErrorEvent

        from core.constants import RUN_FAILED_MESSAGE

        err = RunErrorEvent(type=EventType.RUN_ERROR, message="boom in agent", code="run_failed")

        with _recorded_turn(_mock_agent([_snapshot(merge_request=None), err])) as calls:
            [event async for event in _streamer().events()]

        assert calls == [("start",), ("finalize", False, RUN_FAILED_MESSAGE), _RELEASE]

    async def test_the_run_row_is_named_once_before_the_agents_first_event(self):
        with _recorded_turn(_mock_agent([_snapshot(), _snapshot()])):
            events = [event async for event in _streamer().events()]

        records = [event for event in events if event.type == EventType.CUSTOM and event.name == "run_record"]
        assert (events[0], len(records)) == (records[0], 1)
        assert records[0].value == {"id": "run-pk"}

    async def test_a_failure_before_the_agents_first_event_names_the_run_row_before_the_error(self):
        graph = MagicMock(aget_state=AsyncMock(), aupdate_state=AsyncMock())

        with _recorded_turn(_raising_agent(), graph=graph):
            events = [event async for event in _streamer().events()]

        assert [(event.type, getattr(event, "name", None)) for event in events] == [
            (EventType.CUSTOM, "run_record"),
            (EventType.RUN_ERROR, None),
        ]

    async def test_a_setup_failure_before_the_run_row_only_releases_the_slot(self):
        with _recorded_turn(_mock_agent([]), runtime_ctx=_failing_ctx) as calls:
            events = [event async for event in _streamer().events()]

        assert [(event.type, event.code) for event in events] == [(EventType.RUN_ERROR, "run_failed")]
        assert calls == [_RELEASE]

    async def test_a_lost_lock_stops_the_turn_and_skips_ref_and_watch(self):
        from core.constants import INTERRUPTED_MESSAGE

        with _recorded_turn(_mock_agent([_snapshot(), _snapshot()]), heartbeat=False) as calls:
            events = [event async for event in _streamer().events()]

        assert events[-1].code == "run_interrupted"
        assert calls == [("start",), ("finalize", False, INTERRUPTED_MESSAGE), _RELEASE]

    async def test_a_lost_lock_wins_over_a_cancel_request(self):
        with _recorded_turn(_mock_agent([_snapshot(), _snapshot()]), heartbeat=False, cancel=True):
            events = [event async for event in _streamer().events()]

        assert events[-1].code == "run_interrupted"

    async def test_a_cancel_request_stops_the_turn_and_skips_ref_and_watch(self):
        from core.constants import CANCELLED_BY_USER_MESSAGE

        with _recorded_turn(_mock_agent([_snapshot(), _snapshot()]), cancel=True) as calls:
            events = [event async for event in _streamer().events()]

        assert events[-1].code == "run_cancelled"
        assert calls == [("start",), ("finalize", False, CANCELLED_BY_USER_MESSAGE), _RELEASE]

    async def test_a_hard_cancel_after_a_stop_request_is_recorded_as_stopped(self):
        import asyncio

        from core.constants import CANCELLED_BY_USER_MESSAGE

        started = asyncio.Event()

        async def _hang(_input):
            started.set()
            await asyncio.Event().wait()
            yield  # pragma: no cover - makes this an async generator

        agent = MagicMock()
        agent.run = _hang

        async def _drain():
            async for _ in _streamer().events():
                pass

        with _recorded_turn(agent, cancel=True) as calls:
            task = asyncio.create_task(_drain())
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert calls == [("start",), ("finalize", False, CANCELLED_BY_USER_MESSAGE), _RELEASE]

    async def test_a_heartbeat_error_is_logged_and_keeps_the_turn_running(self, caplog):
        heartbeat = AsyncMock(side_effect=RuntimeError("db down"))

        with (
            _recorded_turn(_mock_agent([_snapshot(), _snapshot()]), heartbeat=heartbeat) as calls,
            caplog.at_level("ERROR", logger="daiv.chat"),
        ):
            events = [event async for event in _streamer().events()]

        assert [event.type for event in events] == [
            EventType.CUSTOM,
            EventType.STATE_SNAPSHOT,
            EventType.STATE_SNAPSHOT,
        ]
        assert heartbeat.await_count == 2
        assert ("finalize", True, "") in calls
        assert "heartbeat failed" in caplog.text

    @pytest.mark.parametrize(
        ("step", "message"), [("persist", "persist session ref"), ("claim", "claim the merge request")]
    )
    async def test_a_failed_ref_or_watch_step_is_logged_and_the_turn_still_succeeds(self, caplog, step, message):
        mr = {"source_branch": "feature-y", "merge_request_id": 42}

        with (
            _recorded_turn(
                _mock_agent([_snapshot(merge_request=mr, published=True)]),
                checkpoint={"merge_request": mr, "published": True},
                errors={step: RuntimeError("db down")},
            ) as calls,
            caplog.at_level("ERROR", logger="daiv.chat"),
        ):
            [event async for event in _streamer().events()]

        assert calls == [("start",), ("persist", "main", mr), ("claim", "run-pk", mr), ("finalize", True, ""), _RELEASE]
        assert f"failed to {message}" in caplog.text

    async def test_a_finalize_failure_is_logged_and_still_releases_the_slot(self, caplog):
        with (
            _recorded_turn(_mock_agent([]), errors={"finalize": RuntimeError("db down")}) as calls,
            caplog.at_level("ERROR", logger="daiv.chat"),
        ):
            [event async for event in _streamer().events()]

        assert calls[-2:] == [("finalize", True, ""), _RELEASE]
        assert "failed to finalize chat run" in caplog.text

    async def test_a_release_failure_is_logged_and_swallowed(self, caplog):
        with (
            _recorded_turn(_mock_agent([]), errors={"release": RuntimeError("db down")}) as calls,
            caplog.at_level("ERROR", logger="daiv.chat"),
        ):
            events = [event async for event in _streamer().events()]

        assert events == []
        assert calls[-1] == _RELEASE
        assert "failed to release run slot" in caplog.text

    async def test_a_reader_that_goes_away_mid_turn_still_ends_the_turn(self):
        from core.constants import INTERRUPTED_MESSAGE

        with _recorded_turn(_mock_agent([_snapshot(), _snapshot()])) as calls:
            events = _streamer().events()
            await anext(events)
            await events.aclose()

        assert calls == [("start",), ("finalize", False, INTERRUPTED_MESSAGE), _RELEASE]

    async def test_a_reader_that_goes_away_with_the_cancel_flag_set_is_recorded_as_stopped(self):
        from core.constants import CANCELLED_BY_USER_MESSAGE

        with _recorded_turn(_mock_agent([_snapshot(), _snapshot()]), cancel=True) as calls:
            events = _streamer().events()
            await anext(events)
            await events.aclose()

        assert calls == [("start",), ("finalize", False, CANCELLED_BY_USER_MESSAGE), _RELEASE]

    async def test_the_stream_heartbeats_the_slot_the_view_claimed(self):
        heartbeat = AsyncMock(return_value=True)

        with _recorded_turn(_mock_agent([_snapshot(), _snapshot()]), heartbeat=heartbeat):
            async for _ in _streamer().events():
                pass

        heartbeat.assert_awaited_with("t-stream", "r-1")

    async def test_the_turns_token_usage_reaches_its_run_row(self):
        from automation.agent import usage_tracking

        handlers, usages = [], []
        track = usage_tracking.track_usage_metadata

        @contextmanager
        def _recording_track():
            with track() as handler:
                handlers.append(handler)
                yield handler

        async def _finalize(_run, *, usage, **_kwargs):
            usages.append(usage)

        summary = MagicMock(return_value=MagicMock(to_dict=lambda: {"total_tokens": 12}))
        with (
            _recorded_turn(_mock_agent([_snapshot()])),
            patch("automation.agent.usage_tracking.track_usage_metadata", _recording_track),
            patch("chat.api.streaming.build_usage_summary", summary),
            patch("chat.api.streaming.finalize_chat_run", side_effect=_finalize),
        ):
            async for _ in _streamer().events():
                pass

        summary.assert_called_once_with(handlers[0])
        assert usages == [{"total_tokens": 12}]

    async def test_a_reader_that_goes_away_while_the_clone_fails_to_close_still_ends_the_turn(self):
        from core.constants import INTERRUPTED_MESSAGE

        def _failing_close(*_args, **_kwargs):
            ctx = _mock_ctx()
            ctx.__aexit__ = AsyncMock(side_effect=OSError("clone cleanup failed"))
            return ctx

        with _recorded_turn(_mock_agent([_snapshot(), _snapshot()]), runtime_ctx=_failing_close) as calls:
            events = _streamer().events()
            await anext(events)
            await events.aclose()

        assert calls == [("start",), ("finalize", False, INTERRUPTED_MESSAGE), _RELEASE]

    async def test_a_turn_closed_before_the_run_starts_still_releases_the_slot(self):
        cloned: list[bool] = []

        def _ctx(*_args, **_kwargs):
            cloned.append(True)
            return _mock_ctx()

        streamer = ChatRunStreamer(
            repo_id="a/b",
            ref="main",
            thread_id="t-stream",
            run_id="r-1",
            input_data=_run_input(),
            auto_resolved_env={"id": "env-1", "name": "Default", "scope": "global"},
        )
        with _recorded_turn(_mock_agent([]), runtime_ctx=_ctx) as calls:
            events = streamer.events()
            first = await anext(events)
            await events.aclose()

        assert first.name == "resolved_env"
        assert cloned == []
        assert calls == [_RELEASE]

    async def test_a_hard_cancel_during_setup_only_releases_the_slot(self):
        import asyncio

        cloning = asyncio.Event()

        def _hanging_ctx(*_args, **_kwargs):
            @asynccontextmanager
            async def _ctx():
                cloning.set()
                await asyncio.Event().wait()
                yield  # pragma: no cover - makes this an async context manager

            return _ctx()

        async def _drain():
            async for _ in _streamer().events():
                pass

        with _recorded_turn(_mock_agent([]), runtime_ctx=_hanging_ctx) as calls:
            task = asyncio.create_task(_drain())
            await asyncio.wait_for(cloning.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert calls == [_RELEASE]

    async def test_events_after_an_emitted_run_error_still_reach_the_client(self):
        from ag_ui.core.events import RunErrorEvent

        from core.constants import RUN_FAILED_MESSAGE

        err = RunErrorEvent(type=EventType.RUN_ERROR, message="boom in agent", code="run_failed")

        with _recorded_turn(_mock_agent([err, _snapshot()])) as calls:
            events = [event async for event in _streamer().events()]

        assert [event.type for event in events] == [EventType.CUSTOM, EventType.RUN_ERROR, EventType.STATE_SNAPSHOT]
        assert calls == [("start",), ("finalize", False, RUN_FAILED_MESSAGE), _RELEASE]


class TestTurnMeasurements:
    """Isolated class so the module-level autouse ``_patch_run_lifecycle`` (which stubs the measurements
    write) can be overridden here — the real ``_record_measurements`` must write to the DB.
    """

    @pytest.fixture(autouse=True)
    def _patch_run_lifecycle(self):
        """Override the module-level autouse: the real ``start_chat_run`` creates the row."""

    @pytest.mark.django_db(transaction=True)
    async def test_a_turn_records_its_clone_time_on_the_run_it_started(self):
        await amake_job_session("t-stream")

        with (
            patch("chat.api.streaming.finalize_chat_run", new=AsyncMock()),
            patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", return_value=_mock_agent([])),
            patch("chat.api.streaming.SessionLock.release", new=AsyncMock()),
            patch("chat.api.streaming.SessionLock.heartbeat", new=AsyncMock()),
        ):
            async for _ in _streamer().events():
                pass

        run = await Run.objects.aget(session_id="t-stream")
        assert run.clone_seconds == 1.5


@pytest.mark.django_db(transaction=True)
class TestInboxContext:
    @pytest.fixture(autouse=True)
    def _patch_run_lifecycle(self):
        """Override the module-level autouse: the turn's ``Run`` row is real, so events can point at it."""

    async def _session_with_taken_events(self) -> Session:
        from sessions.models import EventKind, EventState

        from tests.unit_tests.sessions.inbox.conftest import amake_event

        session = await Session.objects.acreate(
            thread_id="t-stream", origin="chat", repo_id="a/b", ref="main", active_run_id="r-1"
        )
        await amake_event(session, EventKind.MR_MERGED, merge_request_iid=3, state=EventState.TAKEN, claim_token="r-1")  # noqa: S106
        return session

    async def test_the_turns_run_takes_over_the_events_the_view_took(self, real_chat_inbox):
        from sessions.models import InboxEvent

        await self._session_with_taken_events()
        turn = SimpleNamespace(chat_run=None)

        run_id = await _streamer()._start_turn(turn, "main")

        event = await InboxEvent.objects.aget()
        assert (str(event.run_id), event.claim_token) == (run_id, "")

    async def test_their_context_is_written_ahead_of_the_persons_message(self, real_chat_inbox):
        from automation.agent.synthetic import synthetic_kind

        await self._session_with_taken_events()
        turn = SimpleNamespace(chat_run=None)
        streamer = _streamer()
        await streamer._start_turn(turn, "main")
        agent = MagicMock(aupdate_state=AsyncMock())

        await streamer._add_inbox_context(
            turn, SimpleNamespace(agent=agent, config={"configurable": {}}, fast_forward=None)
        )

        [call] = agent.aupdate_state.await_args_list
        [context] = call.args[1]["messages"]
        assert synthetic_kind(context) == "inbox"
        assert "!3 was merged into main" in context.content
        assert call.kwargs == {"as_node": "model"}

    async def test_a_turn_with_nothing_taken_writes_nothing(self, real_chat_inbox):
        await Session.objects.acreate(thread_id="t-stream", origin="chat", repo_id="a/b", ref="main")
        turn = SimpleNamespace(chat_run=None)
        streamer = _streamer()
        await streamer._start_turn(turn, "main")
        agent = MagicMock(aupdate_state=AsyncMock())

        await streamer._add_inbox_context(turn, SimpleNamespace(agent=agent, config={}, fast_forward=None))

        agent.aupdate_state.assert_not_awaited()

    async def test_a_warm_checkout_brought_up_to_the_remote_says_so(self, real_chat_inbox):
        await Session.objects.acreate(thread_id="t-stream", origin="chat", repo_id="a/b", ref="main")
        turn = SimpleNamespace(chat_run=None)
        streamer = _streamer()
        await streamer._start_turn(turn, "main")
        agent = MagicMock(aupdate_state=AsyncMock())

        await streamer._add_inbox_context(turn, SimpleNamespace(agent=agent, config={}, fast_forward="advanced"))

        [context] = agent.aupdate_state.await_args.args[1]["messages"]
        assert "The branch main moved on the remote. Your working copy was fast-forwarded." in context.content

    async def test_a_failed_write_is_logged_and_the_turn_goes_on(self, real_chat_inbox, caplog):
        await self._session_with_taken_events()
        turn = SimpleNamespace(chat_run=None)
        streamer = _streamer()
        await streamer._start_turn(turn, "main")
        agent = MagicMock(aupdate_state=AsyncMock(side_effect=RuntimeError("redis down")))

        with caplog.at_level("ERROR", logger="daiv.chat"):
            await streamer._add_inbox_context(turn, SimpleNamespace(agent=agent, config={}, fast_forward=None))

        assert "could not add the inbox context" in caplog.text


async def test_the_inbox_is_drained_once_the_turn_releases_the_slot(chat_inbox):
    order: list[str] = []
    chat_inbox.drain.side_effect = lambda thread_id: order.append(f"drain {thread_id}")

    async def _release(thread_id, run_id):
        order.append("release")

    with patch("chat.api.streaming.SessionLock.release", _release):
        await _streamer()._end_turn(SimpleNamespace(chat_run=None), succeeded=True)

    assert order == ["release", "drain t-stream"]


async def test_a_failed_release_skips_the_drain(chat_inbox, caplog):
    with patch("chat.api.streaming.SessionLock.release", AsyncMock(side_effect=RuntimeError("db down"))):
        await _streamer()._end_turn(SimpleNamespace(chat_run=None), succeeded=True)

    chat_inbox.drain.assert_not_called()
