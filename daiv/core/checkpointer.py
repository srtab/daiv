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
