from types import SimpleNamespace
from typing import Annotated, NotRequired
from unittest.mock import AsyncMock

import pytest
from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from redis.exceptions import RedisError
from sessions.archive import (
    ARCHIVED_CHANNELS,
    SEED_AS_NODE,
    aarchive_thread,
    aarchived_contexts,
    aload_archived_values,
    archivable_values,
    arestore_thread,
    continues_context,
    decode_values,
    encode_values,
)
from sessions.models import SessionArchive

from codebase.base import MergeRequest, User
from tests.unit_tests.sessions.conftest import amake_job_session, archive_fields


def _merge_request() -> MergeRequest:
    return MergeRequest(
        repo_id="group/repo",
        merge_request_id=7,
        source_branch="daiv/fix",
        target_branch="main",
        title="Fix",
        description="",
        labels=["daiv"],
        web_url="https://gitlab.example/group/repo/-/merge_requests/7",
        sha="b4c9f488efdc044b39083ff605b3920c4d842e41",
        author=User(id=2, name="DAIV", username="daiv"),
        draft=False,
    )


def _state() -> dict:
    return {
        "messages": [
            HumanMessage(content="fix the bug", id="h1"),
            AIMessage(
                content="", id="a1", tool_calls=[{"id": "c1", "name": "bash", "args": {"command": "cat logo.png"}}]
            ),
            ToolMessage(content="\x89PNG\x00\x00", tool_call_id="c1", name="bash", id="t1"),
            AIMessage(content="done", id="a2"),
        ],
        "merge_request": _merge_request(),
        "diff_stats": {"added": 3, "removed": 1},
        "todos": [{"content": "fix the bug", "status": "completed"}],
        "loaded_tool_names": {"web_fetch", "bash"},
        "_summarization_event": {
            "cutoff_index": 2,
            "summary_message": HumanMessage(content="summary", id="s1"),
            "file_path": None,
        },
        "session_id": "sandbox-1",
        "github_token": "ghs_secret",
        "memory_contents": {"AGENTS.md": "stale"},
    }


def test_only_the_archived_channels_are_kept():
    assert set(archivable_values(_state())) == set(ARCHIVED_CHANNELS)


def test_channels_without_a_value_are_left_out():
    assert archivable_values({"messages": [], "merge_request": None}) == {"messages": []}


def test_archived_values_round_trip_through_the_codec():
    values = archivable_values(_state())

    restored = decode_values(*encode_values(values))

    assert restored["messages"] == values["messages"]
    assert restored["messages"][2].content == "\x89PNG\x00\x00"
    assert restored["merge_request"] == values["merge_request"]
    assert restored["loaded_tool_names"] == {"web_fetch", "bash"}
    assert restored["_summarization_event"] == values["_summarization_event"]
    assert (restored["todos"], restored["diff_stats"]) == (values["todos"], values["diff_stats"])


def _checkpointer(values: dict | None = None, *, error: Exception | None = None) -> SimpleNamespace:
    checkpoint = None if values is None else SimpleNamespace(checkpoint={"channel_values": values})
    return SimpleNamespace(aget_tuple=AsyncMock(return_value=checkpoint, side_effect=error))


def _turns(*ids: str) -> list:
    """Alternating user and assistant messages with the given ids."""
    return [
        HumanMessage(content=f"turn {id_}", id=id_) if n % 2 == 0 else AIMessage(content=f"reply {id_}", id=id_)
        for n, id_ in enumerate(ids)
    ]


def test_a_context_continues_while_its_first_message_leads():
    assert continues_context("h1", _turns("h1", "a1")) is True
    assert continues_context("h1", _turns("r1")) is False
    assert continues_context("h1", _turns("h2", "a2", "h1")) is False
    assert continues_context("h1", []) is False
    assert continues_context("", _turns("r1")) is True


@pytest.mark.django_db(transaction=True)
class TestArchiveThread:
    async def test_a_first_run_archives_the_thread_as_context_zero(self):
        thread_id = await amake_job_session()

        written = await aarchive_thread(_checkpointer({"messages": _turns("h1", "a1"), "session_id": "s"}), thread_id)

        assert written is True
        [context] = await aarchived_contexts(thread_id)
        assert (context.epoch, context.first_message_id, context.message_count) == (0, "h1", 2)
        assert "session_id" not in await aload_archived_values(context.pk)

    async def test_a_later_run_of_the_same_context_overwrites_it(self):
        thread_id = await amake_job_session()

        await aarchive_thread(_checkpointer({"messages": _turns("h1", "a1")}), thread_id)
        await aarchive_thread(_checkpointer({"messages": _turns("h1", "a1", "h2", "a2")}), thread_id)

        [context] = await aarchived_contexts(thread_id)
        assert (context.epoch, context.message_count) == (0, 4)

    async def test_a_reset_context_opens_the_next_epoch_and_keeps_the_earlier_one(self):
        thread_id = await amake_job_session()
        cleared = [AIMessage(content="✅ Conversation context cleared successfully.", id="r1")]

        await aarchive_thread(_checkpointer({"messages": _turns("h1", "a1")}), thread_id)
        await aarchive_thread(_checkpointer({"messages": cleared}), thread_id)

        contexts = await aarchived_contexts(thread_id)
        assert [(c.epoch, c.first_message_id) for c in contexts] == [(0, "h1"), (1, "r1")]
        assert [m.id for m in (await aload_archived_values(contexts[0].pk))["messages"]] == ["h1", "a1"]

    async def test_a_thread_that_lost_messages_never_shrinks_its_archive(self, caplog):
        thread_id = await amake_job_session()
        await aarchive_thread(_checkpointer({"messages": _turns("h1", "a1", "h2", "a2")}), thread_id)

        with caplog.at_level("ERROR", logger="daiv.sessions"):
            assert await aarchive_thread(_checkpointer({"messages": _turns("h1", "a1")}), thread_id) is False
            assert "fewer than its archived 4; keeping the archive" in caplog.text
            caplog.clear()
            assert await aarchive_thread(_checkpointer({"messages": _turns("h2", "a2")}), thread_id) is False
            assert "now starts inside its archived context; keeping the archive" in caplog.text

        [context] = await aarchived_contexts(thread_id)
        assert context.message_count == 4

    async def test_a_first_message_sent_again_at_the_end_does_not_continue_its_context(self):
        thread_id = await amake_job_session()
        await aarchive_thread(_checkpointer({"messages": _turns("h1", "a1", "h2", "a2")}), thread_id)
        replayed = _turns("h2", "a2", "h1")

        assert await aarchive_thread(_checkpointer({"messages": replayed}), thread_id) is False
        assert await aarchive_thread(_checkpointer({"messages": [*replayed, *_turns("h3", "a3")]}), thread_id) is False

        [context] = await aarchived_contexts(thread_id)
        assert (context.first_message_id, context.message_count) == ("h1", 4)
        assert [m.id for m in (await aload_archived_values(context.pk))["messages"]] == ["h1", "a1", "h2", "a2"]

    async def test_nothing_is_archived_without_messages(self):
        thread_id = await amake_job_session()

        assert await aarchive_thread(_checkpointer(None), thread_id) is False
        assert await aarchive_thread(_checkpointer({"messages": []}), thread_id) is False
        assert await aarchived_contexts(thread_id) == []

    async def test_a_thread_without_a_session_row_is_skipped(self, caplog):
        with caplog.at_level("WARNING", logger="daiv.sessions"):
            assert await aarchive_thread(_checkpointer({"messages": _turns("h1")}), "no-such-thread") is False

        assert "No session no-such-thread" in caplog.text

    async def test_a_failed_read_is_logged_not_raised(self, caplog):
        thread_id = await amake_job_session()

        with caplog.at_level("ERROR", logger="daiv.sessions"):
            assert await aarchive_thread(_checkpointer(error=RedisError("down")), thread_id) is False

        assert f"Failed to archive thread {thread_id}" in caplog.text


class _ToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class _PrivateState(AgentState):
    loaded_tool_names: NotRequired[Annotated[set[str], PrivateStateAttr]]


class _Probe(AgentMiddleware):
    """Records what each model call saw. Its ``after_model`` hook leaves a step pending after a seeded model write."""

    state_schema = _PrivateState

    def __init__(self):
        super().__init__()
        self.seen: list[tuple[list[str], set[str] | None]] = []

    async def abefore_model(self, state, runtime):
        self.seen.append(([m.id for m in state["messages"]], state.get("loaded_tool_names")))

    async def aafter_model(self, state, runtime):
        return None


def _deep_agent(probe: _Probe, checkpointer: InMemorySaver, *, tools: tuple = (), reply_id: str = "a2"):
    model = _ToolModel(messages=iter([AIMessage(content=f"answer {reply_id}", id=reply_id)]))
    return create_deep_agent(model=model, tools=list(tools), middleware=[probe], checkpointer=checkpointer)


class _ParentChainSaver(InMemorySaver):
    """Replays delta channels through the base class's parent-chain walk, as ``AsyncRedisSaver`` does."""

    def get_delta_channel_history(self, *, config, channels):
        return BaseCheckpointSaver.get_delta_channel_history(self, config=config, channels=channels)

    async def aget_delta_channel_history(self, *, config, channels):
        return await BaseCheckpointSaver.aget_delta_channel_history(self, config=config, channels=channels)


@pytest.mark.django_db(transaction=True)
class TestRestoreThread:
    async def test_an_expired_thread_is_seeded_from_its_latest_context(self):
        thread_id = await amake_job_session()
        await SessionArchive.objects.acreate(session_id=thread_id, **archive_fields(0, _turns("h1", "a1")))
        await SessionArchive.objects.acreate(session_id=thread_id, **archive_fields(1, [AIMessage("cleared", id="r1")]))
        agent = SimpleNamespace(aupdate_state=AsyncMock())
        config = {"configurable": {"thread_id": thread_id}}

        assert await arestore_thread(agent, _checkpointer(None), config) is True

        args, kwargs = agent.aupdate_state.await_args
        assert args[0] is config
        assert [m.id for m in args[1]["messages"]] == ["r1"]
        assert kwargs == {"as_node": SEED_AS_NODE}

    async def test_a_live_checkpoint_is_left_alone(self):
        thread_id = await amake_job_session()
        await SessionArchive.objects.acreate(session_id=thread_id, **archive_fields(0, _turns("h1")))
        agent = SimpleNamespace(aupdate_state=AsyncMock())

        restored = await arestore_thread(
            agent, _checkpointer({"messages": []}), {"configurable": {"thread_id": thread_id}}
        )

        assert restored is False
        agent.aupdate_state.assert_not_awaited()

    async def test_a_thread_without_an_archive_starts_empty(self):
        thread_id = await amake_job_session()
        agent = SimpleNamespace(aupdate_state=AsyncMock())

        assert await arestore_thread(agent, _checkpointer(None), {"configurable": {"thread_id": thread_id}}) is False
        agent.aupdate_state.assert_not_awaited()

    async def test_a_merge_request_that_no_longer_revives_is_dropped(self):
        thread_id = await amake_job_session()
        fields = archive_fields(0, _turns("h1"), merge_request={"repo_id": "group/repo"})
        await SessionArchive.objects.acreate(session_id=thread_id, **fields)
        agent = SimpleNamespace(aupdate_state=AsyncMock())

        await arestore_thread(agent, _checkpointer(None), {"configurable": {"thread_id": thread_id}})

        assert "merge_request" not in agent.aupdate_state.await_args.args[1]

    async def test_a_failed_restore_is_logged_and_the_run_starts_empty(self, caplog):
        thread_id = await amake_job_session()
        await SessionArchive.objects.acreate(session_id=thread_id, **archive_fields(0, _turns("h1")))
        agent = SimpleNamespace(aupdate_state=AsyncMock(side_effect=ValueError("bad update")))

        with caplog.at_level("ERROR", logger="daiv.sessions"):
            restored = await arestore_thread(agent, _checkpointer(None), {"configurable": {"thread_id": thread_id}})

        assert restored is False
        assert f"Failed to restore thread {thread_id}" in caplog.text

    async def test_a_restored_thread_resumes_with_its_history_and_private_state(self):
        thread_id = await amake_job_session()
        fields = archive_fields(0, _turns("h1", "a1"), loaded_tool_names={"web_fetch"})
        await SessionArchive.objects.acreate(session_id=thread_id, **fields)
        probe, saver = _Probe(), InMemorySaver()
        agent = _deep_agent(probe, saver)
        config = {"configurable": {"thread_id": thread_id}}

        assert await arestore_thread(agent, saver, config) is True
        await agent.ainvoke({"messages": [HumanMessage(content="follow-up", id="h2")]}, config)

        assert probe.seen == [(["h1", "a1", "h2"], {"web_fetch"})]

    async def test_a_dangling_tool_call_in_the_archive_is_not_replayed(self):
        calls: list[str] = []

        @tool
        def ping() -> str:
            """Ping."""
            calls.append("ping")
            return "pong"

        thread_id = await amake_job_session()
        dangling = AIMessage(content="", id="a1", tool_calls=[{"id": "c1", "name": "ping", "args": {}}])
        await SessionArchive.objects.acreate(session_id=thread_id, **archive_fields(0, [_turns("h1")[0], dangling]))
        saver = InMemorySaver()
        agent = _deep_agent(_Probe(), saver, tools=(ping,))
        config = {"configurable": {"thread_id": thread_id}}

        assert await arestore_thread(agent, saver, config) is True
        await agent.ainvoke({"messages": [HumanMessage(content="follow-up", id="h2")]}, config)

        assert calls == []

    async def test_a_thread_read_through_the_parent_chain_walk_archives_and_resumes(self):
        thread_id = await amake_job_session()
        config = {"configurable": {"thread_id": thread_id}}
        first = _ParentChainSaver()
        await _deep_agent(_Probe(), first, reply_id="a1").ainvoke(
            {"messages": [HumanMessage(content="first", id="h1")]}, config
        )

        assert "messages" not in (await first.aget_tuple(config)).checkpoint["channel_values"]
        assert await aarchive_thread(first, thread_id) is True
        [context] = await aarchived_contexts(thread_id)
        assert [m.id for m in (await aload_archived_values(context.pk))["messages"]] == ["h1", "a1"]

        probe, fresh = _Probe(), _ParentChainSaver()
        agent = _deep_agent(probe, fresh)
        assert await arestore_thread(agent, fresh, config) is True
        await agent.ainvoke({"messages": [HumanMessage(content="follow-up", id="h2")]}, config)

        assert [ids for ids, _ in probe.seen] == [["h1", "a1", "h2"]]
