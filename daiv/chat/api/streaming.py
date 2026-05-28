from __future__ import annotations

import logging
import time
from dataclasses import dataclass, fields, is_dataclass
from typing import TYPE_CHECKING, Any

from ag_ui.core.events import CustomEvent, EventType, RunErrorEvent
from copilotkit import LangGraphAGUIAgent
from langgraph.store.memory import InMemoryStore

from automation.agent.graph import create_daiv_agent
from automation.agent.mcp.toolkits import MCPToolkit
from automation.agent.middlewares.mcp_session import MCPSessionStateMiddleware
from automation.agent.utils import build_langsmith_config, get_daiv_agent_kwargs
from codebase.base import Scope
from codebase.context import set_runtime_ctx
from core.checkpointer import open_checkpointer

from .event_filter import SubagentEventFilter
from .threads import ChatThreadService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from ag_ui.core import RunAgentInput
    from ag_ui.encoder import EventEncoder

    from codebase.base import MergeRequest
    from codebase.context import RuntimeCtx

logger = logging.getLogger("daiv.chat")

# GitState fields that survive the ag-ui output-schema filter and reach the
# chat client through STATE_SNAPSHOT events.
STREAMED_STATE_KEYS = ("merge_request",)

# Bump ``last_active_at`` at most this often while the stream is alive.
HEARTBEAT_INTERVAL_S = 5.0


async def _aread_mcp_session_ids(checkpointer: Any, thread_id: str) -> dict[str, str]:
    """Return the previous turn's MCP session ids from the checkpointer, or empty.

    Loaded once at stream start so ``MCPToolkit.aopen`` can resume stateful MCP
    sessions (Playwright browser context) instead of opening fresh ones.
    """
    try:
        tup = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
    except Exception:
        logger.warning("chat: failed to load checkpoint for thread_id=%s", thread_id, exc_info=True)
        return {}
    if tup is None:
        return {}
    ids = (tup.checkpoint or {}).get("channel_values", {}).get("mcp_session_ids") or {}
    return {str(k): str(v) for k, v in ids.items()} if isinstance(ids, dict) else {}


class RuntimeContextLangGraphAGUIAgent(LangGraphAGUIAgent):
    """Inject the daiv RuntimeCtx dataclass into upstream's stream kwargs.

    Upstream's ``get_stream_kwargs`` only accepts dict-shaped contexts, but our graph
    declares ``context_schema=RuntimeCtx`` and expects the frozen dataclass itself.
    """

    def __init__(self, *, runtime_context: RuntimeCtx, **kwargs: Any):
        super().__init__(**kwargs)
        self._runtime_context = runtime_context

    def get_stream_kwargs(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        stream_kwargs = super().get_stream_kwargs(*args, **kwargs)
        stream_kwargs.setdefault("context", self._runtime_context)
        return stream_kwargs

    def get_schema_keys(self, config: Any) -> dict[str, list[str]]:
        # Upstream calls ``graph.config_schema().schema()`` which recurses into
        # ``context_schema=RuntimeCtx``. RuntimeCtx holds a ``git.Repo`` field that
        # pydantic cannot turn into JSON schema, so the call raises
        # PydanticInvalidForJsonSchema. Derive context keys from the dataclass directly.
        ctx_schema = getattr(self.graph, "context_schema", None)
        if not is_dataclass(ctx_schema):
            logger.warning(
                "chat: context_schema %r is not a dataclass; STATE_SNAPSHOT context keys will be empty", ctx_schema
            )
            context_keys: list[str] = []
        else:
            context_keys = [f.name for f in fields(ctx_schema)]
        constant = list(self.constant_schema_keys)
        return {"input": constant, "output": [*constant, *STREAMED_STATE_KEYS], "config": [], "context": context_keys}


@dataclass(frozen=True, kw_only=True)
class ChatRunStreamer:
    """SSE generator: configures the agent, runs it through the subagent filter,
    captures the latest MR from STATE_SNAPSHOTs, and persists the ref before
    releasing the per-thread run slot.
    """

    repo_id: str
    ref: str
    thread_id: str
    run_id: str
    input_data: RunAgentInput
    encoder: EventEncoder
    sandbox_environment_id: str | None = None
    agent_model: str | None = None
    agent_thinking_level: str | None = None
    # When set, ``{id, name, scope}`` of the env the view auto-resolved for this run.
    # The chat composer's locked pill is still showing "Auto" on the client; the
    # streamer's first emit swaps it to the real name without waiting for a page
    # refresh. ``None`` skips the emit — the view decides when emission is meaningful.
    auto_resolved_env: dict[str, str] | None = None

    def __post_init__(self) -> None:
        # The view passes thread_id/run_id alongside input_data; a future refactor
        # could desync them silently. Pin the invariant here.
        if self.thread_id != self.input_data.thread_id:
            raise ValueError(f"thread_id mismatch: {self.thread_id!r} vs input_data {self.input_data.thread_id!r}")
        if self.run_id != self.input_data.run_id:
            raise ValueError(f"run_id mismatch: {self.run_id!r} vs input_data {self.input_data.run_id!r}")

    async def events(self) -> AsyncIterator[str]:
        last_mr: MergeRequest | None = None
        clean_run = False
        last_heartbeat = time.monotonic()
        try:
            # Surface the auto-resolved env before any agent output so the locked composer
            # pill swaps "Auto" → real env name as early as possible. Kept inside the
            # ``try`` so an encode failure still routes through RUN_ERROR + ``release_run``
            # in ``finally``; the emit precedes ``set_runtime_ctx`` so the user still sees
            # what would have run even if agent setup fails.
            if self.auto_resolved_env is not None:
                yield self.encoder.encode(
                    CustomEvent(type=EventType.CUSTOM, name="resolved_env", value=self.auto_resolved_env)
                )
            async with (
                open_checkpointer() as checkpointer,
                set_runtime_ctx(
                    repo_id=self.repo_id, scope=Scope.GLOBAL, ref=self.ref, sandbox_env_id=self.sandbox_environment_id
                ) as runtime_ctx,
            ):
                # Load MCP session ids from the previous turn's checkpoint so
                # stateful servers (Playwright above all) resume their browser
                # context instead of starting fresh; the middleware below writes
                # the current ids back to state for the next turn.
                prev_session_ids = await _aread_mcp_session_ids(checkpointer, self.thread_id)
                async with MCPToolkit.aopen(session_ids=prev_session_ids) as (mcp_tools, current_session_ids):
                    agent_kwargs = get_daiv_agent_kwargs(
                        model_config=runtime_ctx.config.models.agent,
                        agent_model=self.agent_model,
                        agent_thinking_level=self.agent_thinking_level,
                    )
                    agent = await create_daiv_agent(
                        ctx=runtime_ctx,
                        mcp_tools=mcp_tools,
                        checkpointer=checkpointer,
                        store=InMemoryStore(),
                        middleware=[MCPSessionStateMiddleware(current_session_ids)],
                        **agent_kwargs,
                    )
                langsmith_config = build_langsmith_config(
                    runtime_ctx,
                    trigger="chat",
                    model=agent_kwargs["model_names"][0],
                    thinking_level=agent_kwargs["thinking_level"],
                    extra_metadata={"override_source": "explicit" if self.agent_model else None},
                )
                langgraph_agent = RuntimeContextLangGraphAGUIAgent(
                    name="DAIV",
                    description="DAIV agent",
                    graph=agent,
                    config={"recursion_limit": 500, **langsmith_config},
                    runtime_context=runtime_ctx,
                )
                async for event in SubagentEventFilter().apply(langgraph_agent.run(self.input_data)):
                    if event.type == EventType.STATE_SNAPSHOT:
                        snap = getattr(event, "snapshot", None) or {}
                        if isinstance(snap, dict) and "merge_request" in snap:
                            last_mr = snap["merge_request"]
                    yield self.encoder.encode(event)

                    now = time.monotonic()
                    if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                        last_heartbeat = now
                        try:
                            await ChatThreadService.heartbeat(self.thread_id, self.run_id)
                        except Exception:
                            logger.exception("chat: heartbeat failed for thread_id=%s", self.thread_id)
                clean_run = True
        except Exception:
            logger.exception("Chat run failed for thread_id=%s run_id=%s", self.thread_id, self.run_id)
            yield self.encoder.encode(
                RunErrorEvent(
                    type=EventType.RUN_ERROR, message="Run failed. Check server logs for details.", code="run_failed"
                )
            )
        finally:
            # Both cleanup steps are wrapped: a post-stream DB hiccup must not
            # retroactively paint a clean run as RUN_ERROR, and a release_run
            # failure must not leave the per-thread slot permanently claimed.
            # ref is only persisted on a clean finish — a partial run could have
            # checked out a branch without committing, and pinning it would
            # silently retarget reloads at half-built state.
            if clean_run:
                try:
                    await ChatThreadService.persist_ref(self.thread_id, self.ref, last_mr)
                except Exception:
                    logger.exception("chat: failed to persist thread ref for thread_id=%s", self.thread_id)
            try:
                await ChatThreadService.release_run(self.thread_id, self.run_id)
            except Exception:
                logger.exception("chat: failed to release run slot for thread_id=%s", self.thread_id)
