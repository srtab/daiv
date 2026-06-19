from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from django.conf import settings

from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.checkpoint.redis.jsonplus_redis import JsonPlusRedisSerializer
from pydantic import BaseModel

from codebase.base import MergeRequest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


# Domain pydantic models that may live in checkpointed agent state. Listing a model
# here lets DAIVRedisSerializer both encode it to RedisJSON and revive it on read.
# Encode is generic (``_default_handler`` wraps any plain pydantic model), so a new
# checkpointed domain model round-trips even if unlisted — but append it here anyway to
# keep it on the documented decode allowlist (``allowed_json_modules``).
CHECKPOINT_JSON_TYPES: tuple[type, ...] = (MergeRequest,)


class DAIVRedisSerializer(JsonPlusRedisSerializer):
    """Redis checkpoint serializer that round-trips DAIV domain pydantic models.

    ``langgraph-checkpoint-redis==0.4.1`` encodes non-JSON-native objects through
    ``self._encode_constructor_args`` -- a method ``langgraph-checkpoint==4.1.1``
    removed in its GHSA-fjqc-hq36-qh5p hardening. So any plain pydantic model in
    checkpointed state (e.g. ``GitState.merge_request``, a :class:`MergeRequest`)
    blows up RedisJSON serialization with ``TypeError: not JSON serializable`` and
    fails the run when the checkpointer tries to persist agent state.

    On the happy path decode round-trips: the redis read path (``_revive_if_needed``)
    reconstructs the ``lc:2`` envelope for any importable class via
    ``_reconstruct_from_constructor``. But that reviver swallows reconstruction failures
    (``except Exception: pass``) and returns the raw envelope ``dict`` -- so if a model's
    schema drifts across a deploy (a field renamed/required/retyped, or the class relocated),
    a checkpointed model silently comes back as a ``dict`` with no log. Consumers must therefore
    not assume the revived value is the model: ``GitMiddleware`` guards its ``merge_request``
    read (``_state_merge_request``) and fails loud rather than letting an ``AttributeError``
    surface far downstream. We also register our models on ``allowed_json_modules`` -- but note
    the read path taken here (``_reconstruct_from_constructor``) does **not** consult that
    allowlist; it is kept only so the *documented* decode gate (``_revive_lc2``) stays correct
    should a future upstream route the read path through it, not as a runtime guarantee today.
    LangChain objects (messages) carry ``to_json`` and keep flowing through the parent's safe
    path untouched.

    Sets need a matching decode override (``_reviver``). The adapter encodes a Python ``set``
    as an ``lc:2`` envelope keyed by ``kwargs["__set_items__"]``, but the base reviver routes
    ``("builtins", "set")`` (a SAFE_MSGPACK_TYPE) through ``_revive_lc2`` → ``set(**kwargs)`` →
    ``TypeError``, which ``langgraph-checkpoint>=4.1.1`` turns into ``None`` -- silently nulling
    every checkpointed set (e.g. ``loaded_tool_names``). No published adapter/base pairing
    avoids this, so ``_reviver`` reconstructs the envelope before delegating.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("allowed_json_modules", CHECKPOINT_JSON_TYPES)
        super().__init__(**kwargs)

    def _reviver(self, value: Any) -> Any:
        """Reconstruct adapter-encoded sets before the base reviver corrupts them to ``None``.

        ``langgraph-checkpoint-redis==0.4.1`` serializes a ``set`` as
        ``{"lc": 2, "type": "constructor", "id": ["builtins", "set"], "kwargs": {"__set_items__": [...]}}``
        (``JsonPlusRedisSerializer._preprocess_interrupts``); only the adapter's own
        ``_reconstruct_from_constructor`` understands ``__set_items__``. But the read path
        (``_revive_if_needed``) runs *this* base reviver first, and ``set(**kwargs)`` raises,
        so the value comes back ``None`` -- the adapter fallback never fires because it only
        triggers when the base returns the *unchanged* dict. Intercept the envelope here.
        ``frozenset`` is handled for symmetry though DAIV stores only plain ``set``.
        """
        if (
            isinstance(value, dict)
            and value.get("lc") == 2
            and value.get("type") == "constructor"
            and value.get("id") in (["builtins", "set"], ["builtins", "frozenset"])
            and isinstance(value.get("kwargs"), dict)
            and "__set_items__" in value["kwargs"]
        ):
            items = (self._revive_if_needed(item) for item in value["kwargs"]["__set_items__"])
            return frozenset(items) if value["id"][-1] == "frozenset" else set(items)
        return super()._reviver(value)

    def _default_handler(self, obj: Any) -> Any:
        # Only intercept plain pydantic models (those without ``to_json``). The parent
        # ``JsonPlusRedisSerializer._default_handler`` handles LangChain objects,
        # Interrupt/Send and bytes itself; its fall-through branch for plain pydantic
        # models is the one calling the removed ``_encode_constructor_args``, so this
        # fills exactly that gap. LangChain messages are pydantic too but carry
        # ``to_json``, so the guard yields them to the parent's safe path.
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
