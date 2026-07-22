from __future__ import annotations

import logging
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

logger = logging.getLogger("daiv.checkpointer")

# ``_DeltaSnapshot`` wraps a full channel value when ``DeltaChannel`` writes a periodic
# snapshot into ``channel_values`` (see ``aresolve_thread_messages`` and the serializer's
# round-trip overrides below). It is a beta langgraph type; import defensively so a relocation
# can't break module import. But the degradation is NOT free: with ``_DeltaSnapshot`` (and thus
# ``_DELTA_SNAPSHOT_ID``) ``None`` the round-trip overrides below no-op, so if langgraph still
# emits the wrapper the encode path silently reverts to the stock bare-array behaviour that
# caused Sentry DAIV-1S. We therefore log loudly at import so a disabled fix is visible rather
# than resurfacing later as an untraceable ``NotImplementedError`` crash.
try:
    from langgraph.checkpoint.serde.types import _DeltaSnapshot
except ImportError:  # pragma: no cover - defensive against langgraph beta churn
    _DeltaSnapshot = None  # ty: ignore[invalid-assignment]
    logger.error(
        "langgraph._DeltaSnapshot is no longer importable from langgraph.checkpoint.serde.types; "
        "DAIVRedisSerializer's _DeltaSnapshot round-trip is DISABLED. If DeltaChannel still emits "
        "it, checkpoints regress to the Sentry DAIV-1S 'Message as a sequence must be (role "
        "string, template)' crash. Update the import path."
    )

# The ``lc:2`` constructor ``id`` that ``_encode_constructor_envelope(_DeltaSnapshot, ...)`` emits on
# encode -- module split into parts, matching the base builder's format (NOT the dotted 2-element
# form ``_default_handler`` uses, which exists only to match the class-based ``allowed_json_modules``
# entry -- a concern ``_DeltaSnapshot`` doesn't share). Kept as a module constant purely for the
# decode-side matcher ``_is_delta_snapshot_envelope``: the saver's ``_recursive_deserialize`` routes
# ONLY ``lc``-constructor dicts to ``serde._revive_if_needed``, so this exact id is what the
# channel-values read path hands back to us to reconstruct.
_DELTA_SNAPSHOT_ID: list[str] | None = (
    [*_DeltaSnapshot.__module__.split("."), _DeltaSnapshot.__name__] if _DeltaSnapshot is not None else None
)


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

    **``_DeltaSnapshot`` needs an override.** deepagents >= 0.6 stores ``messages`` in a
    langgraph ``DeltaChannel`` whose periodic snapshot blob is a ``_DeltaSnapshot`` NamedTuple
    living in ``channel_values``. The stock redis serializer has no ``_DeltaSnapshot`` support:
    its ``_preprocess_interrupts`` treats the NamedTuple as a plain tuple, orjson serialises it
    as a bare JSON array ``[value]``, and the read path returns a nested list ``[[msg, ...]]``
    with the wrapper lost. langgraph later feeds that value to ``DeltaChannel.from_checkpoint``
    as a seed, the double-nesting survives, and ``convert_to_messages([[msg, ...]])`` raises
    ``NotImplementedError: Message as a sequence must be (role string, template)`` (Sentry
    DAIV-1S). The overrides below round-trip ``_DeltaSnapshot`` through an ``lc:2`` constructor
    envelope: ``_preprocess_interrupts`` emits it on encode, and ``_revive_if_needed``
    reconstructs it on decode. One decode override covers both read paths, because the saver's
    ``_recursive_deserialize`` routes inline ``channel_values`` ``lc`` dicts to
    ``serde._revive_if_needed`` and ``loads_typed`` calls it directly for blobs. This is an
    upstream gap in ``langgraph-checkpoint-redis`` 0.5.1 -- worth reporting -- but the fix
    belongs here so production stops crashing regardless of upstream timing.

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

    def _preprocess_interrupts(self, obj: Any) -> Any:
        # ``_DeltaSnapshot`` is a NamedTuple, so the stock pre-pass would fall into its
        # ``(list, tuple)`` branch and rebuild it as a plain tuple -- orjson then emits a bare
        # JSON array and the read path returns a nested list, silently dropping the wrapper
        # (Sentry DAIV-1S). Intercept it here, BEFORE delegating to the parent, and emit an
        # ``lc:2`` constructor envelope via the inherited ``_encode_constructor_envelope`` (the same
        # builder the parent uses for its own ``set``/dataclass branches): that is the ONLY dict
        # shape the saver's ``_recursive_deserialize`` hands back to ``serde._revive_if_needed``
        # (below) on the channel-values read path. The wrapped value is processed recursively so the
        # messages it holds still encode through the parent path.
        if _DeltaSnapshot is not None and isinstance(obj, _DeltaSnapshot):
            return self._encode_constructor_envelope(
                _DeltaSnapshot, kwargs={"value": self._preprocess_interrupts(obj.value)}
            )
        return super()._preprocess_interrupts(obj)

    def _revive_if_needed(self, obj: Any) -> Any:
        # Reconstruct the ``_DeltaSnapshot`` envelope emitted above before the parent's generic
        # ``lc`` handling runs (which would route this id through the base reviver). This single
        # override covers BOTH decode paths: the saver's ``_recursive_deserialize`` delegates
        # ``lc`` dicts here for inline ``channel_values``, and ``loads_typed`` calls it for blobs.
        if self._is_delta_snapshot_envelope(obj):
            return _DeltaSnapshot(self._revive_if_needed(obj["kwargs"]["value"]))
        return super()._revive_if_needed(obj)

    @staticmethod
    def _is_delta_snapshot_envelope(obj: Any) -> bool:
        """True if ``obj`` is the ``lc:2`` constructor envelope produced for a ``_DeltaSnapshot``.

        Encode always emits ``lc:2``; ``lc:1`` is accepted only to mirror the base reviver's own
        ``revived.get("lc") in (1, 2)`` tolerance and the saver's ``_recursive_deserialize`` routing
        gate. The exact ``_DELTA_SNAPSHOT_ID`` match is what actually discriminates -- and its
        ``is not None`` short-circuit is load-bearing: without it a ``None`` id (langgraph relocated
        ``_DeltaSnapshot``, see the import guard) would match any id-less envelope and route it to
        ``_DeltaSnapshot(...)`` == ``None(...)``.
        """
        return (
            _DELTA_SNAPSHOT_ID is not None
            and isinstance(obj, dict)
            and obj.get("lc") in (1, 2)
            and obj.get("type") == "constructor"
            and obj.get("id") == _DELTA_SNAPSHOT_ID
            and isinstance(obj.get("kwargs"), dict)
            and "value" in obj["kwargs"]
        )


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
    """Unwrap a ``DeltaChannel`` snapshot to its stored value; pass anything else through.

    Two encodings are unwrapped:

    * A live ``_DeltaSnapshot`` NamedTuple -- what the serializer round-trip (``_preprocess_interrupts``
      / ``_revive_if_needed``) reconstructs for checkpoints written after that fix shipped -- yields
      its ``.value``.
    * The *lossy legacy* form from a checkpoint written BEFORE that fix: the stock serializer emitted
      the single-field NamedTuple as a bare JSON array ``[value]``, so a ``messages`` snapshot comes
      back doubly-nested as ``[[msg, ...]]`` with the wrapper lost. Such checkpoints still live in
      Redis until their TTL expires, so reading one (e.g. rendering an old session in the dashboard)
      must unwrap the inner list rather than feed ``[[msg, ...]]`` to ``add_messages`` -- which unpacks
      the inner list as ``(role, template)`` and raises ``NotImplementedError: Message as a sequence
      must be (role string, template)`` (Sentry DAIV-1S / DAIV-1T).

    The lossy heuristic (length-1 list whose sole element is a list) is unambiguous for the ``messages``
    channel this helper serves: a genuine messages list holds messages (objects or ``lc`` envelope
    dicts), never lists, so it never misfires -- and a lossy empty snapshot ``[[]]`` correctly unwraps
    to ``[]``. A genuine list is returned unchanged (identity preserved) so the inline fast-path caller
    can keep returning it as-is.
    """
    if _DeltaSnapshot is not None and isinstance(value, _DeltaSnapshot):
        return value.value
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], list):
        return value[0]
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
