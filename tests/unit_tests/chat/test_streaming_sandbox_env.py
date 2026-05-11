"""Verifies ``ChatRunStreamer`` threads ``sandbox_environment_id`` through
``set_runtime_ctx`` as ``sandbox_env_id``. The runtime resolver consumes that
kwarg to load the per-run override row — if the streamer drops it, every chat
run silently falls back to the global default."""

from contextlib import asynccontextmanager, suppress
from unittest.mock import MagicMock, patch

import pytest

from chat.api.streaming import ChatRunStreamer


def test_chat_run_streamer_dataclass_accepts_sandbox_env_id():
    streamer = ChatRunStreamer(
        repo_id="r/p",
        ref="",
        thread_id="t",
        run_id="r",
        input_data=MagicMock(thread_id="t", run_id="r"),
        encoder=MagicMock(),
        sandbox_environment_id="env-uuid",
    )
    assert streamer.sandbox_environment_id == "env-uuid"


@pytest.mark.asyncio
async def test_streamer_passes_env_id_into_set_runtime_ctx():
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
        encoder=MagicMock(encode=lambda e: "x"),
        sandbox_environment_id="env-uuid",
    )
    with (
        patch("chat.api.streaming.set_runtime_ctx", _fake_set_runtime_ctx),
        patch("chat.api.streaming.open_checkpointer", _fake_open_checkpointer),
        patch("chat.api.streaming.create_daiv_agent", MagicMock(return_value=MagicMock())),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent", MagicMock()),
        patch("chat.api.streaming.SubagentEventFilter", MagicMock()),
        patch("chat.api.streaming.build_langsmith_config", return_value={}),
        suppress(Exception),
    ):
        async for _ in streamer.events():
            break
    assert captured.get("sandbox_env_id") == "env-uuid"
