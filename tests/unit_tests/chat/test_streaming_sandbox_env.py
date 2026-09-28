"""Verifies ``ChatRunStreamer`` hands ``sandbox_environment_id`` to the executor, which builds the run's sandbox
spec from it. If the streamer drops it, every chat run silently falls back to the global default."""

from contextlib import asynccontextmanager, suppress
from unittest.mock import MagicMock, patch

import pytest

from chat.api.streaming import ChatRunStreamer
from tests.unit_tests.conftest import stub_sandbox_spec


def test_chat_run_streamer_dataclass_accepts_sandbox_env_id():
    streamer = ChatRunStreamer(
        repo_id="r/p",
        ref="",
        thread_id="t",
        run_id="r",
        input_data=MagicMock(thread_id="t", run_id="r"),
        sandbox_environment_id="env-uuid",
    )
    assert streamer.sandbox_environment_id == "env-uuid"


@pytest.mark.asyncio
async def test_streamer_emits_resolved_env_custom_event_when_auto_resolved():
    """When the view auto-resolved an env and supplied ``auto_resolved_env``, the
    first yielded event must be a CUSTOM ``resolved_env`` event so the chat
    client can swap "Auto" → real env name without waiting for a refresh."""
    emitted = []

    @asynccontextmanager
    async def _fake_set_runtime_ctx(repo_id, **kwargs):
        yield MagicMock(config=MagicMock(models=MagicMock(agent=object())))

    @asynccontextmanager
    async def _fake_open_checkpointer():
        yield MagicMock()

    streamer = ChatRunStreamer(
        repo_id="r/p",
        ref="main",
        thread_id="t",
        run_id="r",
        input_data=MagicMock(thread_id="t", run_id="r"),
        auto_resolved_env={"id": "env-uuid", "name": "Default", "scope": "global"},
    )
    with (
        patch("codebase.context.set_runtime_ctx", _fake_set_runtime_ctx),
        patch("core.checkpointer.open_checkpointer", _fake_open_checkpointer),
        patch("automation.agent.graph.create_daiv_agent", MagicMock(return_value=MagicMock())),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", MagicMock()),
        patch("chat.api.streaming.SubagentEventFilter", MagicMock()),
        patch("automation.agent.utils.build_langsmith_config", return_value={}),
        suppress(Exception),
    ):
        async for event in streamer.events():
            emitted.append(event)
            break

    assert emitted, "streamer yielded nothing"
    first = emitted[0]
    assert first.type.value == "CUSTOM"
    assert first.name == "resolved_env"
    assert first.value == {"id": "env-uuid", "name": "Default", "scope": "global"}


@pytest.mark.asyncio
async def test_streamer_skips_resolved_env_emit_when_not_auto_resolved():
    """No ``auto_resolved_env`` means an explicit pick or existing-thread submit —
    the locked pill already shows the right name client-side, no emit needed."""
    emitted = []

    @asynccontextmanager
    async def _fake_set_runtime_ctx(repo_id, **kwargs):
        yield MagicMock(config=MagicMock(models=MagicMock(agent=object())))

    @asynccontextmanager
    async def _fake_open_checkpointer():
        yield MagicMock()

    streamer = ChatRunStreamer(
        repo_id="r/p",
        ref="main",
        thread_id="t",
        run_id="r",
        input_data=MagicMock(thread_id="t", run_id="r"),
        auto_resolved_env=None,
    )
    with (
        patch("codebase.context.set_runtime_ctx", _fake_set_runtime_ctx),
        patch("core.checkpointer.open_checkpointer", _fake_open_checkpointer),
        patch("automation.agent.graph.create_daiv_agent", MagicMock(return_value=MagicMock())),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", MagicMock()),
        patch("chat.api.streaming.SubagentEventFilter", MagicMock()),
        patch("automation.agent.utils.build_langsmith_config", return_value={}),
        suppress(Exception),
    ):
        async for event in streamer.events():
            emitted.append(event)

    # No CUSTOM "resolved_env" event should appear (any other events are unrelated mocks).
    assert not any(
        getattr(e, "type", None) and e.type.value == "CUSTOM" and getattr(e, "name", "") == "resolved_env"
        for e in emitted
    )


@pytest.mark.asyncio
async def test_streamer_builds_the_sandbox_spec_from_its_env_id():
    captured = {}

    @asynccontextmanager
    async def _fake_set_runtime_ctx(repo_id, **kwargs):
        captured.update(kwargs)
        yield MagicMock(config=MagicMock(models=MagicMock(agent=object())))

    @asynccontextmanager
    async def _fake_open_checkpointer():
        yield MagicMock()

    streamer = ChatRunStreamer(
        repo_id="r/p",
        ref="main",
        thread_id="t",
        run_id="r",
        input_data=MagicMock(thread_id="t", run_id="r"),
        sandbox_environment_id="env-uuid",
    )
    with (
        stub_sandbox_spec() as build_spec,
        patch("codebase.context.set_runtime_ctx", _fake_set_runtime_ctx),
        patch("core.checkpointer.open_checkpointer", _fake_open_checkpointer),
        patch("automation.agent.graph.create_daiv_agent", MagicMock(return_value=MagicMock())),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", MagicMock()),
        patch("chat.api.streaming.SubagentEventFilter", MagicMock()),
        patch("automation.agent.utils.build_langsmith_config", return_value={}),
        suppress(Exception),
    ):
        async for _ in streamer.events():
            break
    build_spec.assert_awaited_once_with("env-uuid")
    assert captured.get("sandbox_spec") is build_spec.return_value
