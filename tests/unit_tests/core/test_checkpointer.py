"""Tests for the checkpoint serializer that lets DAIV domain models survive Redis.

The agent stores a :class:`~codebase.base.MergeRequest` in checkpointed state
(``GitState.merge_request``). ``DAIVRedisSerializer`` encodes plain pydantic models
via ``model_dump(mode="json")`` (cleaner nested-model handling than the stock
``__dict__`` path). ``set`` values (``loaded_tool_names``) used to be nulled by the
stock reviver and needed a bespoke override; ``langgraph-checkpoint-redis`` 0.5.1 fixed
that upstream, so the set tests below now guard the *stock* round-trip against a
downgrade or regression.
"""

from __future__ import annotations

import dataclasses

import orjson
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.redis.jsonplus_redis import JsonPlusRedisSerializer

from codebase.base import MergeRequest, User
from core.checkpointer import DAIVRedisSerializer


@dataclasses.dataclass(frozen=True)
class _RunningSummary:
    """A module-level dataclass standing in for the kind written to checkpoint state by
    middleware (e.g. a summarization running-summary). Must be importable so the adapter's
    read path (``_reconstruct_from_constructor``) can revive it by module + name."""

    summary: str
    summarized_message_ids: list[str]


@pytest.fixture
def merge_request() -> MergeRequest:
    return MergeRequest(
        repo_id="srtab/daiv2",
        merge_request_id=30,
        source_branch="feat/echo-slash-command-8",
        target_branch="main",
        title="Add EchoSlashCommand for text echo functionality",
        description="Introduces a new slash command that echoes back provided text.",
        labels=["daiv"],
        web_url="http://127.0.0.1:8929/srtab/daiv2/-/merge_requests/30",
        sha="b4c9f488efdc044b39083ff605b3920c4d842e41",
        author=User(id=2, name="DAIV", username="daiv"),
        draft=False,
    )


def test_encodes_pydantic_model_as_constructor_envelope(merge_request):
    encoded = DAIVRedisSerializer()._default_handler(merge_request)

    assert encoded["lc"] == 2
    assert encoded["type"] == "constructor"
    # Module kept dotted so it matches the class-based allowed_json_modules entry.
    assert encoded["id"] == ["codebase.base", "MergeRequest"]
    assert encoded["kwargs"]["merge_request_id"] == 30
    assert encoded["kwargs"]["author"]["username"] == "daiv"


def test_merge_request_round_trips_through_serde_contract(merge_request):
    """Through the public serde API the saver actually calls (``dumps_typed``/``loads_typed``)."""
    serde = DAIVRedisSerializer()

    type_, blob = serde.dumps_typed({"merge_request": merge_request})
    restored = serde.loads_typed((type_, blob))

    assert isinstance(restored["merge_request"], MergeRequest)
    assert restored["merge_request"] == merge_request


def test_merge_request_round_trips_through_redis_read_path(merge_request):
    """Encode exactly as the RedisJSON dump does, then revive via the read path."""
    serde = DAIVRedisSerializer()

    raw = orjson.dumps({"merge_request": merge_request}, default=serde._default_handler, option=orjson.OPT_NON_STR_KEYS)
    doc = orjson.loads(raw)

    revived = serde._revive_if_needed(doc["merge_request"])
    assert isinstance(revived, MergeRequest)
    assert revived == merge_request


def test_minimally_populated_merge_request_round_trips():
    """The common production shape: optional fields unset (``web_url``/``sha``/``name`` None)."""
    serde = DAIVRedisSerializer()
    minimal = MergeRequest(
        repo_id="srtab/daiv2",
        merge_request_id=1,
        source_branch="b",
        target_branch="main",
        title="t",
        description="d",
        author=User(id=1, username="someone"),
    )

    type_, blob = serde.dumps_typed({"mr": minimal})
    restored = serde.loads_typed((type_, blob))["mr"]

    assert restored == minimal
    assert restored.web_url is None
    assert restored.sha is None
    assert restored.labels == []
    assert restored.author.name is None


def test_none_merge_request_is_unaffected():
    """The common case (no MR resolved yet) stays plain JSON."""
    raw = orjson.dumps({"merge_request": None}, default=DAIVRedisSerializer()._default_handler)
    assert orjson.loads(raw) == {"merge_request": None}


def test_objects_with_to_json_are_not_intercepted():
    """Pydantic objects that carry ``to_json`` (LangChain messages) must go to the parent
    path, not our domain-model branch."""
    encoded = DAIVRedisSerializer()._default_handler(HumanMessage(content="hi"))

    # Encoded by the parent's safe path, not wrapped in our ``codebase.base`` envelope.
    assert encoded.get("id", [None])[0] != "codebase.base"


def test_stock_redis_serializer_round_trips_sets():
    """Regression guard: the stock serializer used to corrupt sets to ``None``.

    Historically the adapter encoded a set under a ``kwargs["__set_items__"]`` envelope, and
    the base reviver routed ``builtins.set`` through ``_revive_lc2`` (``set(**kwargs)`` →
    ``TypeError``), which ``langgraph-checkpoint>=4.1.1`` swallowed by returning ``None`` --
    silently nulling the set. ``langgraph-checkpoint-redis`` 0.5.1 fixed this: the stock
    serializer now encodes sets via the ``args`` constructor envelope and ``_revive_if_needed``
    reconstructs them (``_reconstruct_set_constructor`` still accepts the legacy
    ``__set_items__`` form too), so a round-trip is lossless. The guard is kept (asserting the
    fixed behaviour) so a downgrade or upstream regression is caught here rather than silently
    corrupting production checkpoints.
    """
    stock = JsonPlusRedisSerializer(allowed_json_modules=[("codebase.base", "MergeRequest")])

    restored = stock.loads_typed(stock.dumps_typed({"loaded_tool_names": {"Read", "Edit"}}))

    assert restored == {"loaded_tool_names": {"Read", "Edit"}}


def test_set_round_trips_through_serde_contract():
    """Through the public serde API the saver calls; the stock reviver reconstructs the set (0.5.1)."""
    serde = DAIVRedisSerializer()
    original = {"loaded_tool_names": {"Read", "Edit", "Grep"}}

    restored = serde.loads_typed(serde.dumps_typed(original))

    assert restored == original
    assert isinstance(restored["loaded_tool_names"], set)


def test_empty_set_round_trips():
    """``loaded_tool_names`` starts empty before any deferred tool is loaded."""
    serde = DAIVRedisSerializer()

    restored = serde.loads_typed(serde.dumps_typed({"loaded_tool_names": set()}))

    assert restored == {"loaded_tool_names": set()}
    assert isinstance(restored["loaded_tool_names"], set)


def test_set_round_trips_through_redis_read_path(merge_request):
    """Encode exactly as the RedisJSON dump does, then revive via the read path. A set nested
    alongside a domain model must both survive."""
    serde = DAIVRedisSerializer()
    payload = {"loaded_tool_names": {"Read", "Edit"}, "merge_request": merge_request}

    raw = orjson.dumps(serde._preprocess_interrupts(payload), default=serde._default_handler)
    revived = serde._revive_if_needed(orjson.loads(raw))

    assert revived["loaded_tool_names"] == {"Read", "Edit"}
    assert isinstance(revived["loaded_tool_names"], set)
    assert isinstance(revived["merge_request"], MergeRequest)
    assert revived["merge_request"] == merge_request


def test_dataclass_in_writes_does_not_crash_encoding():
    """The crash path itself: ``dumps_typed`` over a dataclass must not raise."""
    serde = DAIVRedisSerializer()

    type_, _ = serde.dumps_typed({"running_summary": _RunningSummary("done", ["m1"])})

    assert type_ == "json"


def test_dataclass_round_trips_through_serde_contract():
    """Through the public serde API the saver calls; the constructor envelope is read back
    via the adapter's ``_reconstruct_from_constructor`` path."""
    serde = DAIVRedisSerializer()
    original = {"running_summary": _RunningSummary("compacted 5 msgs", ["m1", "m2", "m3"])}

    restored = serde.loads_typed(serde.dumps_typed(original))

    assert isinstance(restored["running_summary"], _RunningSummary)
    assert restored == original


def test_dataclass_nested_alongside_set_and_model_round_trips(merge_request):
    """A dataclass, a set, and a domain model in the same write must all survive together --
    the realistic shape of a checkpoint write that mixes channel updates."""
    serde = DAIVRedisSerializer()
    payload = {
        "running_summary": _RunningSummary("s", ["m1"]),
        "loaded_tool_names": {"Read", "Edit"},
        "merge_request": merge_request,
    }

    restored = serde.loads_typed(serde.dumps_typed(payload))

    assert restored["running_summary"] == payload["running_summary"]
    assert restored["loaded_tool_names"] == {"Read", "Edit"}
    assert isinstance(restored["loaded_tool_names"], set)
    assert restored["merge_request"] == merge_request


# ---------------------------------------------------------------------------
# aresolve_thread_messages: DeltaChannel-aware messages read
#
# deepagents >= 0.6 stores ``messages`` in a langgraph ``DeltaChannel`` whose value is
# usually ABSENT from ``channel_values`` (present only on periodic snapshot steps). The
# helper reconstructs the accumulated list from the delta write history so the transcript
# survives a reload. These tests pin the three resolution branches.
# ---------------------------------------------------------------------------


def _history_saver(history):
    """A saver stub whose ``aget_delta_channel_history`` returns ``history``."""
    from unittest.mock import AsyncMock, MagicMock

    saver = MagicMock()
    saver.aget_delta_channel_history = AsyncMock(return_value=history)
    return saver


async def test_resolve_messages_returns_inline_list_without_history_walk():
    """deepagents < 0.6 (plain add_messages) keeps the full list inline; return as-is and
    never touch the (expensive, beta) delta-history contract."""
    from core.checkpointer import aresolve_thread_messages

    msgs = [HumanMessage(content="hi", id="h1")]
    saver = _history_saver({})

    result = await aresolve_thread_messages(saver, {"configurable": {"thread_id": "t"}}, {"messages": msgs})

    assert result is msgs
    saver.aget_delta_channel_history.assert_not_awaited()


async def test_resolve_messages_reconstructs_from_write_history_when_absent():
    """The core bug: ``messages`` absent from ``channel_values`` (DeltaChannel non-snapshot
    step) must be rebuilt by folding the ancestor writes, not read as empty."""
    from langchain_core.messages import AIMessage

    from core.checkpointer import aresolve_thread_messages

    writes = [
        ("task-0", "messages", [HumanMessage(content="q", id="h1")]),
        ("task-1", "messages", [AIMessage(content="a", id="a1")]),
    ]
    saver = _history_saver({"messages": {"writes": writes}})

    # channel_values has other channels but NO messages key -> reconstruction path.
    result = await aresolve_thread_messages(saver, {"configurable": {"thread_id": "t"}}, {"session_id": "x"})

    assert [m.id for m in result] == ["h1", "a1"]
    assert [m.content for m in result] == ["q", "a"]


async def test_resolve_messages_folds_seed_plus_writes():
    """A snapshot ancestor supplies the ``seed``; later writes fold on top (and id-dedup
    replaces an in-place update rather than duplicating)."""
    from langchain_core.messages import AIMessage

    from core.checkpointer import aresolve_thread_messages

    seed = [HumanMessage(content="q", id="h1"), AIMessage(content="partial", id="a1")]
    writes = [("task-2", "messages", [AIMessage(content="final", id="a1")])]
    saver = _history_saver({"messages": {"seed": seed, "writes": writes}})

    result = await aresolve_thread_messages(saver, {"configurable": {"thread_id": "t"}}, {})

    assert [m.id for m in result] == ["h1", "a1"]
    assert result[1].content == "final"  # id-dedup replaced the partial in place


async def test_resolve_messages_empty_history_returns_empty_list():
    """No seed and no writes (nothing recoverable) -> empty list, so callers still flag the
    genuinely-empty case rather than crashing."""
    from core.checkpointer import aresolve_thread_messages

    saver = _history_saver({"messages": {"writes": []}})

    result = await aresolve_thread_messages(saver, {"configurable": {"thread_id": "t"}}, {})

    assert result == []


# ---------------------------------------------------------------------------
# _DeltaSnapshot round-trip (Sentry DAIV-1S)
#
# deepagents >= 0.6 declares ``messages`` as a langgraph ``DeltaChannel`` with
# ``snapshot_frequency=50``. Every 50 message updates ``create_checkpoint`` writes a
# ``_DeltaSnapshot(value=[msg, ...])`` into ``channel_values["messages"]``. The stock
# ``JsonPlusRedisSerializer`` has NO ``_DeltaSnapshot`` support -- it treats the NamedTuple
# as a plain tuple, orjson serialises it as a bare JSON array ``[value]``, and the read path
# returns a nested list ``[[msg, ...]]`` with the wrapper lost. When langgraph later feeds
# that value to ``DeltaChannel.from_checkpoint`` as a seed, the double-nesting survives and
# ``_messages_delta_reducer`` -> ``convert_to_messages([[msg, ...]])`` raises
# ``NotImplementedError: Message as a sequence must be (role string, template)``.
# ``DAIVRedisSerializer`` must round-trip ``_DeltaSnapshot`` losslessly.
# ---------------------------------------------------------------------------


def _recursive_deserialize(serde, doc):
    """Decode ``doc`` via the real ``BaseRedisSaver._recursive_deserialize`` bound onto a minimal
    object carrying only ``.serde`` -- the channel-values read path a stored snapshot actually
    takes, without a live Redis. (The ``__bytes__`` branch that would touch ``_decode_blob`` is
    unused for our input.) Exercising the real method guards the ``lc``-dict delegation contract."""
    from langgraph.checkpoint.redis.base import BaseRedisSaver

    class _SaverStub:
        _recursive_deserialize = BaseRedisSaver._recursive_deserialize

        def __init__(self, serde):
            self.serde = serde

    return _SaverStub(serde)._recursive_deserialize(doc)


def test_delta_snapshot_round_trips_through_serde_contract():
    """Through the public serde API the saver calls (``dumps_typed``/``loads_typed``): the
    ``_DeltaSnapshot`` wrapper and its inner message list must both survive."""
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.serde.types import _DeltaSnapshot

    serde = DAIVRedisSerializer()
    snap = _DeltaSnapshot([HumanMessage(content="q", id="h1"), AIMessage(content="a", id="a1")])

    restored = serde.loads_typed(serde.dumps_typed({"messages": snap}))["messages"]

    assert isinstance(restored, _DeltaSnapshot)
    assert [m.id for m in restored.value] == ["h1", "a1"]
    assert [m.content for m in restored.value] == ["q", "a"]


def test_delta_snapshot_round_trips_through_redis_read_path():
    """Encode exactly as the RedisJSON checkpoint dump does, then revive via the channel-values
    read path a stored snapshot actually takes: ``BaseRedisSaver._recursive_deserialize``, which
    routes ``lc`` constructor dicts to ``serde._revive_if_needed``. Exercising the real saver
    method (bound onto a minimal stub -- no Redis needed) guards the delegation contract."""
    from langgraph.checkpoint.serde.types import _DeltaSnapshot

    serde = DAIVRedisSerializer()
    snap = _DeltaSnapshot([HumanMessage(content="q", id="h1")])

    # As stored in RedisJSON: the whole checkpoint's channel_values dict, encoded via the dump path.
    raw = orjson.dumps(serde._preprocess_interrupts({"messages": snap}), default=serde._default_handler)

    revived = _recursive_deserialize(serde, orjson.loads(raw))

    assert isinstance(revived["messages"], _DeltaSnapshot)
    assert [m.id for m in revived["messages"].value] == ["h1"]


def test_round_tripped_delta_snapshot_seed_reconstructs_without_crash():
    """The exact Sentry DAIV-1S crash, end-to-end through the production chain: a snapshot is
    stored + read back via the channel-values path (``_recursive_deserialize``), then used as a
    ``DeltaChannel`` seed. ``from_checkpoint`` + ``replay_writes`` must fold cleanly and NOT raise
    ``NotImplementedError`` from a double-nested value."""
    from deepagents._messages_reducer import _messages_delta_reducer
    from langchain_core.messages import AIMessage
    from langgraph.channels.delta import DeltaChannel
    from langgraph.checkpoint.serde.types import _DeltaSnapshot

    serde = DAIVRedisSerializer()
    snap = _DeltaSnapshot([HumanMessage(content="q", id="h1")])

    # Store + read back exactly as the checkpoint save/load does: dump channel_values to
    # RedisJSON, then decode via the saver's channel-values path (which yields the delta seed).
    raw = orjson.dumps(serde._preprocess_interrupts({"messages": snap}), default=serde._default_handler)
    seed = _recursive_deserialize(serde, orjson.loads(raw))["messages"]

    channel = DeltaChannel(_messages_delta_reducer, list, snapshot_frequency=50)
    channel.key = "messages"
    replay = channel.from_checkpoint(seed)
    replay.replay_writes([("task-0", "messages", [AIMessage(content="a", id="a1")])])

    assert [m.id for m in replay.get()] == ["h1", "a1"]


def test_delta_snapshot_nested_alongside_model_and_set_round_trips(merge_request):
    """A realistic snapshot checkpoint: ``messages`` as a ``_DeltaSnapshot`` sitting next to a
    domain model and a set in the same ``channel_values`` must all survive together."""
    from langgraph.checkpoint.serde.types import _DeltaSnapshot

    serde = DAIVRedisSerializer()
    payload = {
        "messages": _DeltaSnapshot([HumanMessage(content="q", id="h1")]),
        "merge_request": merge_request,
        "loaded_tool_names": {"Read", "Edit"},
    }

    restored = serde.loads_typed(serde.dumps_typed(payload))

    assert isinstance(restored["messages"], _DeltaSnapshot)
    assert [m.id for m in restored["messages"].value] == ["h1"]
    assert restored["merge_request"] == merge_request
    assert restored["loaded_tool_names"] == {"Read", "Edit"}


def test_delta_snapshot_overrides_degrade_safely_when_type_unimportable(monkeypatch):
    """If langgraph relocates ``_DeltaSnapshot`` (import guard trips -> both module refs ``None``),
    the overrides must cleanly no-op -- never call ``_DeltaSnapshot(...)`` == ``None(...)``. Coverage
    can't catch this: the guarded lines execute either way, so pin the two guards explicitly.

    * decode: ``_is_delta_snapshot_envelope`` short-circuits on the ``None`` id, so an id-less
      constructor look-alike is rejected instead of routed to ``None(...)`` -- drop the
      ``_DELTA_SNAPSHOT_ID is not None`` clause and this assertion ``TypeError``s.
    * encode: ``_preprocess_interrupts`` falls through to the stock ``(list, tuple)`` branch, so our
      ``lc:2`` envelope is NOT produced (the fix silently disables -- hence the loud import log).
    """
    from langgraph.checkpoint.serde.types import _DeltaSnapshot

    import core.checkpointer as cp

    serde = DAIVRedisSerializer()
    real_snap = _DeltaSnapshot([HumanMessage(content="q", id="h1")])

    monkeypatch.setattr(cp, "_DeltaSnapshot", None)
    monkeypatch.setattr(cp, "_DELTA_SNAPSHOT_ID", None)

    # The exact shape that would hit ``None(...)`` if the id short-circuit were ever dropped.
    idless_lookalike = {"lc": 2, "type": "constructor", "kwargs": {"value": [HumanMessage(content="q", id="h1")]}}
    assert serde._is_delta_snapshot_envelope(idless_lookalike) is False

    # Real wrapper no longer intercepted -> stock tuple output, not our dict envelope.
    assert isinstance(serde._preprocess_interrupts(real_snap), tuple)
