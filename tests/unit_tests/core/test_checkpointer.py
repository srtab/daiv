"""Tests for the checkpoint serializer that lets DAIV domain models survive Redis.

The agent stores a :class:`~codebase.base.MergeRequest` in checkpointed state
(``GitState.merge_request``). ``langgraph-checkpoint-redis==0.4.1`` cannot JSON-encode
it -- its ``_default_handler`` calls ``_encode_constructor_args``, a method that
``langgraph-checkpoint==4.1.1`` removed in its GHSA-fjqc-hq36-qh5p hardening -- so
without our custom serializer the checkpoint put raises ``TypeError: not JSON
serializable``, failing ``address_mr_comments_task`` mid-run.
"""

from __future__ import annotations

import orjson
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.redis.jsonplus_redis import JsonPlusRedisSerializer

from codebase.base import MergeRequest, User
from core.checkpointer import DAIVRedisSerializer


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


def test_stock_redis_serializer_cannot_encode_merge_request(merge_request):
    """Regression guard: reproduce the exact production crash with the stock serializer."""
    stock = JsonPlusRedisSerializer()
    with pytest.raises(TypeError, match="not JSON serializable"):
        orjson.dumps({"merge_request": merge_request}, default=stock._default_handler)


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


def test_stock_redis_serializer_loses_sets_on_round_trip():
    """Regression guard: reproduce the production set→None corruption with the stock serializer.

    The adapter encodes a set via ``kwargs["__set_items__"]``, but the base reviver routes
    ``builtins.set`` through ``_revive_lc2`` (``set(**kwargs)`` → ``TypeError``), which
    ``langgraph-checkpoint>=4.1.1`` swallows by returning ``None`` -- silently nulling the set.
    """
    stock = JsonPlusRedisSerializer(allowed_json_modules=[("codebase.base", "MergeRequest")])

    restored = stock.loads_typed(stock.dumps_typed({"loaded_tool_names": {"Read", "Edit"}}))

    assert restored == {"loaded_tool_names": None}  # corrupted


def test_set_round_trips_through_serde_contract():
    """Through the public serde API the saver calls; our ``_reviver`` reconstructs the set."""
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
