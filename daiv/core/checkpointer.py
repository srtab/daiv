from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from django.conf import settings

from langchain_core.messages import AnyMessage  # noqa: TC002
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.checkpoint.redis.jsonplus_redis import JsonPlusRedisSerializer
from langgraph.graph.message import add_messages
from pydantic import BaseModel

from codebase.base import MergeRequest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from langchain_core.runnables import RunnableConfig

# ``_DeltaSnapshot`` wraps a full channel value when ``DeltaChannel`` writes a periodic
# snapshot into ``channel_values`` (see ``aresolve_thread_messages``). It is a beta
# langgraph type; import defensively so a future relocation degrades to the write-replay
# path rather than breaking module import.
try:
    from langgraph.checkpoint.serde.types import _DeltaSnapshot
except ImportError:  # pragma: no cover - defensive against langgraph beta churn
    _DeltaSnapshot = None  # ty: ignore[invalid-assignment]


# Domain pydantic models that may live in checkpointed agent state. Listing a model
# here lets DAIVRedisSerializer both encode it to RedisJSON and revive it on read.
# Encode is generic (``_default_handler`` wraps any plain pydantic model), so a new
# checkpointed domain model round-trips even if unlisted — but append it here anyway to
# keep it on the documented decode allowlist (``allowed_json_modules``).
CHECKPOINT_JSON_TYPES: tuple[type, ...] = (MergeRequest,)


class DAIVRedisSerializer(JsonPlusRedisSerializer):
    """Redis checkpoint serializer that round-trips DAIV domain pydantic models.

    The stock ``_default_handler`` encodes plain pydantic models from ``obj.__dict__``; this
    class overrides it to emit ``model_dump(mode="json")`` so nested models and types serialise
    cleanly (the agent stores a :class:`~codebase.base.MergeRequest` in ``GitState.merge_request``).
    LangChain objects (messages) carry ``to_json`` and keep flowing through the parent's safe path
    untouched.

    **Sets no longer need an override here.** A checkpointed ``set`` (e.g. ``loaded_tool_names``)
    was silently nulled by older ``JsonPlusRedisSerializer`` revivers; ``langgraph-checkpoint-redis``
    0.5.1 reconstructs it upstream in ``_revive_if_needed``, so the bespoke ``_reviver`` this class
    used to carry is gone. ``tests/unit_tests/core/test_checkpointer.py`` guards the round-trip
    (mechanism detailed there) so a downgrade or upstream regression fails loudly rather than
    nulling production sets.

    On the happy path decode round-trips: the redis read path (``_revive_if_needed``)
    reconstructs the ``lc:2`` envelope for any importable class by delegating to the base
    reviver (``_reviver``). But that reviver can fall back to returning the raw envelope
    ``dict`` on a reconstruction failure -- so if a model's schema drifts across a deploy (a
    field renamed/required/retyped, or the class relocated), a checkpointed model silently
    comes back as a ``dict`` with no log. Consumers must therefore not assume the revived value
    is the model: ``GitMiddleware`` guards its ``merge_request`` read (``_state_merge_request``)
    and fails loud rather than letting an ``AttributeError`` surface far downstream. We also
    register our models on ``allowed_json_modules`` -- but note the read path taken here
    (``_revive_if_needed`` → the base reviver) does **not** consult that allowlist; it is kept
    only so the *documented* decode gate (``_revive_lc2``) stays correct should a future upstream
    route the read path through it, not as a runtime guarantee today.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("allowed_json_modules", CHECKPOINT_JSON_TYPES)
        super().__init__(**kwargs)

    def _default_handler(self, obj: Any) -> Any:
        # Intercept plain pydantic models (those without ``to_json``) and encode them
        # via ``model_dump(mode="json")`` rather than the stock ``__dict__`` path, so
        # nested models and types serialise cleanly. LangChain messages are pydantic
        # too but carry ``to_json``, so the guard yields them to the parent's safe path.
        if isinstance(obj, BaseModel) and not hasattr(obj, "to_json"):
            cls = type(obj)
            return {
                "lc": 2,
                "type": "constructor",
                # Module kept dotted (not split into parts) so it matches the
                # class-based allowlist entry ``(cls.__module__, cls.__name__)`` and
                # the reviver imports it cleanly.
                "id": [cls.__module__, cls.__name__],
                "kwargs": obj.model_dump(mode="json"),
            }
        return super()._default_handler(obj)


@asynccontextmanager
async def open_checkpointer() -> AsyncIterator[AsyncRedisSaver]:
    """Yield a configured AsyncRedisSaver using project settings.

    Single source of truth for the Redis connection + TTL. The default serializer is
    swapped for :class:`DAIVRedisSerializer` so domain pydantic models in agent state
    survive the checkpoint round-trip.
    """
    async with AsyncRedisSaver.from_conn_string(
        settings.DJANGO_REDIS_CHECKPOINT_URL, ttl={"default_ttl": settings.DJANGO_REDIS_CHECKPOINT_TTL_MINUTES}
    ) as cp:
        cp.serde = DAIVRedisSerializer()
        yield cp


def _unwrap_delta_snapshot(value: Any) -> Any:
    """Unwrap a ``DeltaChannel`` snapshot to its stored value; pass anything else through."""
    if _DeltaSnapshot is not None and isinstance(value, _DeltaSnapshot):
        return value.value
    return value


async def aresolve_thread_messages(
    cp: AsyncRedisSaver, config: RunnableConfig, channel_values: dict[str, Any]
) -> list[AnyMessage]:
    """Return a thread's full ``messages`` list, reconstructing ``DeltaChannel`` state.

    deepagents >= 0.6 declares the agent's ``messages`` key as langgraph's (beta)
    ``DeltaChannel``. Its ``checkpoint()`` returns a sentinel on non-snapshot supersteps,
    so ``messages`` is **absent** from the checkpoint's ``channel_values`` on essentially
    every finished chat (snapshots only land every ~50 message updates). The accumulated
    list must instead be replayed from ancestor writes via the
    ``aget_delta_channel_history`` contract. ``langgraph-checkpoint-redis`` 0.5.1 does not
    reconstruct delta channels inside ``aget_tuple`` (by design — that is what keeps the
    checkpoint O(N)), so a raw ``channel_values["messages"]`` read comes back empty and
    the transcript renders blank on reload.

    Resolution order:

    * ``messages`` resolves to a list in ``channel_values`` (a plain inline list from
      deepagents < 0.6 ``add_messages``, or a ``_DeltaSnapshot`` wrapping one) — return it.
    * otherwise (absent on a non-snapshot delta step) — replay ``seed + writes`` from the
      delta history.

    The replay folds writes through the public ``add_messages`` reducer. deepagents' own
    ``_messages_delta_reducer`` is ``add_messages`` semantics (id-dedup, ``RemoveMessage``
    tombstones, ``REMOVE_ALL_MESSAGES`` reset) minus per-chunk coercion — and only full
    messages are ever checkpointed — so the reconstruction is identical without importing
    that internal symbol. Returns ``[]`` when nothing is recoverable.
    """
    raw = _unwrap_delta_snapshot(channel_values.get("messages"))
    if isinstance(raw, list):
        return raw

    # ``channel_values`` is passed in (already fetched by the caller) so the inline path
    # above serves legacy threads without a walk; only the delta path pays for the history
    # replay, which re-reads the latest tuple internally to seed its parent-chain walk.
    history = await cp.aget_delta_channel_history(config=config, channels=["messages"])
    entry = history.get("messages") or {}
    seed = _unwrap_delta_snapshot(entry.get("seed"))
    messages: list[AnyMessage] = list(seed) if isinstance(seed, list) else []
    # PendingWrite is ``(task_id, channel, value)``; fold each channel write oldest-to-newest.
    for write in entry.get("writes") or []:
        messages = add_messages(messages, write[2])
    return messages
