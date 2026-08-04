from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage, HumanMessage
from memory.schemas import ExtractedObservations
from sessions.models import Run, RunStatus, Session, SessionOrigin


def _enabled_config(enabled=True):
    config = MagicMock()
    config.memory.enabled = enabled
    return config


def _site_settings(**overrides):
    """Mock of the site-settings singleton with the memory defaults the task reads."""
    ss = MagicMock()
    ss.memory_enabled = True
    ss.memory_extraction_model_name = "openrouter:openai/gpt-5.4-mini"
    ss.memory_extraction_fallback_model_name = "openrouter:anthropic/claude-haiku-4.5"
    for key, value in overrides.items():
        setattr(ss, key, value)
    return ss


def _checkpointer_with(messages):
    tup = None
    if messages is not None:
        tup = MagicMock()
        tup.checkpoint = {"channel_values": {"messages": messages}}

    cp = MagicMock()
    cp.aget_tuple = AsyncMock(return_value=tup)

    @asynccontextmanager
    async def _open():
        yield cp

    return _open


def _checkpointer_with_delta(writes):
    """A checkpointer whose ``messages`` is a DeltaChannel: absent from ``channel_values``,
    recoverable only via ``aget_delta_channel_history``."""
    tup = MagicMock()
    tup.checkpoint = {"channel_values": {"session_id": "x"}}  # no messages key

    cp = MagicMock()
    cp.aget_tuple = AsyncMock(return_value=tup)
    cp.aget_delta_channel_history = AsyncMock(return_value={"messages": {"writes": writes}})

    @asynccontextmanager
    async def _open():
        yield cp

    return _open


def _structured_llm_returning(observations=None, *, error=None):
    llm = MagicMock()
    if error is not None:
        llm.with_config.return_value.ainvoke = AsyncMock(side_effect=error)
    else:
        llm.with_config.return_value.ainvoke = AsyncMock(
            return_value=ExtractedObservations(observations=observations or [])
        )
    return llm


async def _create_run(**kwargs):
    session = await Session.objects.acreate(
        thread_id=kwargs.pop("thread_id", "thread-1"),
        origin=SessionOrigin.API_JOB,
        repo_id=kwargs.get("repo_id", "group/project"),
    )
    defaults = {"trigger_type": SessionOrigin.API_JOB, "repo_id": "group/project", "status": RunStatus.SUCCESSFUL}
    defaults.update(kwargs)
    return await Run.objects.acreate(session=session, **defaults)


TRANSCRIPT = [HumanMessage(content="fix the bug"), AIMessage(content="done, ran make test")]
