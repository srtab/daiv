import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, NotRequired
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from redis.exceptions import RedisError
from sessions.archive import (
    ARCHIVED_CHANNELS,
    ArchiveRestoreError,
    aarchive_thread,
    aarchived_contexts,
    aload_archived_values,
    archivable_values,
    archive_row,
    arestore_thread,
    continues_context,
    decode_values,
    merge_messages,
)

from automation.agent.agent_settings import RunOverrides
from automation.agent.deferred.conf import settings as deferred_settings
from automation.agent.graph import create_daiv_agent
from tests.unit_tests.conftest import FakeArtifactStore, FakeWorkspace, agent_settings, site_snapshot
from tests.unit_tests.sessions.conftest import acreate_archive, amake_job_session, fake_checkpointer, make_merge_request


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
        "merge_request": make_merge_request(),
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


GOLDEN_ROWS = Path(__file__).parent / "data" / "archive_rows.json"


def test_the_sandbox_id_credentials_and_memory_are_never_archived():
    archived = archivable_values(_state())

    assert {"messages", "merge_request", "loaded_tool_names", "_summarization_event"} <= set(archived)
    assert not {"session_id", "github_token", "memory_contents"} & set(archived)


def test_channels_without_a_value_are_left_out():
    assert archivable_values({"messages": [], "merge_request": None}) == {"messages": []}


def test_archived_values_round_trip_through_the_codec():
    values = archivable_values(_state())

    row = archive_row(values)
    restored = decode_values(row["state_type"], row["state"])

    assert restored["messages"] == values["messages"]
    assert restored["messages"][2].content == "\x89PNG\x00\x00"
    assert restored["merge_request"] == values["merge_request"]
    assert restored["loaded_tool_names"] == {"web_fetch", "bash"}
    assert restored["_summarization_event"] == values["_summarization_event"]
    assert (restored["todos"], restored["diff_stats"]) == (values["todos"], values["diff_stats"])
    assert (row["first_message_id"], row["message_count"]) == ("h1", 4)


def test_a_channel_no_longer_archived_is_dropped_on_read():
    row = archive_row({"messages": _turns("h1"), "github_token": "ghs_secret"})

    assert decode_values(row["state_type"], row["state"]) == {"messages": _turns("h1")}


@pytest.mark.parametrize("messages", [[], [HumanMessage(content="no id")]], ids=["no-messages", "first-without-id"])
def test_a_row_needs_messages_whose_first_has_an_id(messages):
    with pytest.raises(ValueError, match="needs messages"):
        archive_row({"messages": messages})


def _golden_values(*, binary: bool) -> dict:
    """What ``data/archive_rows.json`` holds; ``binary`` adds the bytes that switch the serializer to msgpack."""
    values = archivable_values(_state())
    if binary:
        artifact = ToolMessage(content="screenshot", artifact=b"\x89PNG\x00", tool_call_id="c2", name="shot", id="t2")
        values["messages"] = [*values["messages"], artifact]
    return values


@pytest.mark.parametrize("case", json.loads(GOLDEN_ROWS.read_text()), ids=lambda case: case["state_type"])
def test_rows_written_by_earlier_releases_still_decode(case):
    """Rows outlive serializer upgrades: a release that can no longer read these breaks every archived session."""
    restored = decode_values(case["state_type"], base64.b64decode(case["state"]))

    assert restored == _golden_values(binary=case["state_type"] == "msgpack")


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


def _ids(messages: list) -> list[str]:
    return [m.id for m in messages]


def test_a_live_thread_that_lost_its_oldest_writes_is_merged_after_them():
    """The first message, resent after the expiry, comes back at the end of the live thread; it keeps its place."""
    archived = _turns("x", "a1", "h2", "a2")

    merged = merge_messages(archived, _turns("h2", "a2", "x", "h3"))

    assert _ids(merged) == ["x", "a1", "h2", "a2", "h3"]


def test_a_live_thread_that_shares_no_first_message_follows_the_whole_context():
    assert _ids(merge_messages(_turns("h1", "a1"), _turns("h2", "a1", "a2"))) == ["h1", "a1", "h2", "a2"]


def test_the_live_copy_of_a_message_both_hold_wins():
    archived = [HumanMessage(content="old", id="h1"), AIMessage(content="reply", id="a1")]

    merged = merge_messages(archived, [AIMessage(content="redacted", id="a1"), HumanMessage(content="next", id="h2")])

    assert [(m.id, m.content) for m in merged] == [("h1", "old"), ("a1", "redacted"), ("h2", "next")]


async def _contexts(thread_id: str) -> list[tuple[int, list[str], bool]]:
    """Each archived context of the session as ``(epoch, message ids, closed)``."""
    return [
        (context.epoch, _ids((await aload_archived_values(context.pk))["messages"]), context.closed)
        for context in await aarchived_contexts(thread_id)
    ]


@pytest.mark.django_db(transaction=True)
class TestArchiveThread:
    async def test_a_first_run_archives_the_thread_as_context_zero(self):
        thread_id = await amake_job_session()

        written = await aarchive_thread(
            fake_checkpointer({"messages": _turns("h1", "a1"), "session_id": "s"}), thread_id
        )

        assert written is True
        [context] = await aarchived_contexts(thread_id)
        assert (context.epoch, context.first_message_id, context.message_count, context.closed) == (0, "h1", 2, False)
        assert "session_id" not in await aload_archived_values(context.pk)

    async def test_a_later_run_of_the_same_context_overwrites_it(self):
        thread_id = await amake_job_session()

        await aarchive_thread(fake_checkpointer({"messages": _turns("h1", "a1")}), thread_id)
        await aarchive_thread(fake_checkpointer({"messages": _turns("h1", "a1", "h2", "a2")}), thread_id)

        assert await _contexts(thread_id) == [(0, ["h1", "a1", "h2", "a2"], False)]

    async def test_a_clear_closes_the_context_and_the_next_one_reusing_its_first_id_opens_a_new_epoch(self):
        """An issue thread resends its issue message, under the same id, as the first message of every run."""
        thread_id = await amake_job_session()
        await aarchive_thread(fake_checkpointer({"messages": _turns("x", "a1", "h2", "a2")}), thread_id)

        assert await aarchive_thread(fake_checkpointer({"messages": []}), thread_id, reset=True) is False
        await aarchive_thread(fake_checkpointer({"messages": _turns("x", "a3")}), thread_id)

        assert await _contexts(thread_id) == [(0, ["x", "a1", "h2", "a2"], True), (1, ["x", "a3"], False)]

    async def test_a_cleared_thread_that_kept_the_reply_opens_the_next_epoch_with_it(self):
        thread_id = await amake_job_session()
        await aarchive_thread(fake_checkpointer({"messages": _turns("h1", "a1")}), thread_id)

        cleared = [AIMessage(content="✅ Conversation context cleared successfully.", id="r1")]
        assert await aarchive_thread(fake_checkpointer({"messages": cleared}), thread_id, reset=True) is True

        assert await _contexts(thread_id) == [(0, ["h1", "a1"], True), (1, ["r1"], False)]

    async def test_a_clear_that_never_ran_leaves_the_context_open(self):
        """The run raised before the command answered, so the thread still holds the context's messages."""
        thread_id = await amake_job_session()
        await aarchive_thread(fake_checkpointer({"messages": _turns("h1", "a1")}), thread_id)

        await aarchive_thread(fake_checkpointer({"messages": _turns("h1", "a1", "h2")}), thread_id, reset=True)

        assert await _contexts(thread_id) == [(0, ["h1", "a1", "h2"], False)]

    async def test_each_clear_opens_one_more_epoch_and_keeps_every_earlier_context(self):
        thread_id = await amake_job_session()
        await aarchive_thread(fake_checkpointer({"messages": _turns("x", "a1")}), thread_id)
        await aarchive_thread(fake_checkpointer({"messages": []}), thread_id, reset=True)
        await aarchive_thread(fake_checkpointer({"messages": _turns("x", "a2", "h3", "a3")}), thread_id)
        await aarchive_thread(fake_checkpointer({"messages": []}), thread_id, reset=True)
        await aarchive_thread(fake_checkpointer({"messages": _turns("x", "a4")}), thread_id)

        assert await _contexts(thread_id) == [
            (0, ["x", "a1"], True),
            (1, ["x", "a2", "h3", "a3"], True),
            (2, ["x", "a4"], False),
        ]

    async def test_a_thread_that_lost_its_oldest_writes_keeps_extending_its_context(self):
        thread_id = await amake_job_session()
        await aarchive_thread(fake_checkpointer({"messages": _turns("x", "a1", "h2", "a2")}), thread_id)

        await aarchive_thread(fake_checkpointer({"messages": _turns("h2", "a2", "x", "a3")}), thread_id)
        await aarchive_thread(fake_checkpointer({"messages": _turns("h2", "a2", "x", "a3", "h4", "a4")}), thread_id)

        assert await _contexts(thread_id) == [(0, ["x", "a1", "h2", "a2", "a3", "h4", "a4"], False)]

    async def test_a_thread_that_lost_messages_never_shrinks_its_archive(self, caplog):
        thread_id = await amake_job_session()
        await aarchive_thread(fake_checkpointer({"messages": _turns("h1", "a1", "h2", "a2")}), thread_id)

        with caplog.at_level("ERROR", logger="daiv.sessions"):
            assert await aarchive_thread(fake_checkpointer({"messages": _turns("h1", "a1")}), thread_id) is False

        assert "fewer than its archived 4; keeping the archive" in caplog.text
        assert await _contexts(thread_id) == [(0, ["h1", "a1", "h2", "a2"], False)]

    async def test_nothing_is_archived_without_messages(self):
        thread_id = await amake_job_session()

        assert await aarchive_thread(fake_checkpointer(None), thread_id) is False
        assert await aarchive_thread(fake_checkpointer({"messages": []}), thread_id) is False
        assert await aarchived_contexts(thread_id) == []

    async def test_a_thread_without_a_session_row_is_skipped(self, caplog):
        with caplog.at_level("WARNING", logger="daiv.sessions"):
            assert await aarchive_thread(fake_checkpointer({"messages": _turns("h1")}), "no-such-thread") is False

        assert "No session no-such-thread" in caplog.text

    async def test_a_failed_read_is_raised(self):
        thread_id = await amake_job_session()

        with pytest.raises(RedisError, match="down"):
            await aarchive_thread(fake_checkpointer(error=RedisError("down")), thread_id)


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


class _Clear(AgentMiddleware):
    """Answers ``/clear`` as ``SlashCommandMiddleware`` does: the thread is deleted, then the messages are reset."""

    def __init__(self, saver: InMemorySaver, thread_id: str):
        super().__init__()
        self.saver, self.thread_id = saver, thread_id

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(self, state, runtime):
        if state["messages"][-1].content != "/clear":
            return None
        await self.saver.adelete_thread(self.thread_id)
        reply = AIMessage(content="cleared", id="r1")
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), reply], "jump_to": "end"}


def _deep_agent(
    probe: _Probe, checkpointer: InMemorySaver, *, tools: tuple = (), reply_id: str = "a2", middleware: tuple = ()
):
    model = _ToolModel(messages=iter([AIMessage(content=f"answer {reply_id}", id=reply_id)]))
    return create_deep_agent(model=model, tools=list(tools), middleware=[*middleware, probe], checkpointer=checkpointer)


class _ParentChainSaver(InMemorySaver):
    """Replays delta channels through the base class's parent-chain walk, as ``AsyncRedisSaver`` does."""

    def get_delta_channel_history(self, *, config, channels):
        return BaseCheckpointSaver.get_delta_channel_history(self, config=config, channels=channels)

    async def aget_delta_channel_history(self, *, config, channels):
        return await BaseCheckpointSaver.aget_delta_channel_history(self, config=config, channels=channels)


async def _daiv_agent(checkpointer: BaseCheckpointSaver | None = None):
    """The DAIV agent with every optional middleware on, so its state holds every channel a run can checkpoint."""
    settings = agent_settings(
        site=site_snapshot(web_search_enabled=True, web_fetch_enabled=True),
        run=RunOverrides(model_names=("primary", "fallback")),
    )
    ctx = MagicMock()
    ctx.config.context_file_name = "AGENTS.md"

    def _tool_model(**kwargs):
        return _ToolModel(messages=iter([]))

    with (
        patch("automation.agent.graph.get_model", side_effect=_tool_model),
        patch("automation.agent.subagents.get_model", side_effect=_tool_model),
        patch("automation.agent.graph.load_custom_subagents", AsyncMock(return_value=[])),
        patch("automation.agent.middlewares.sandbox.site_settings", sandbox_api_key="key"),
        patch.object(deferred_settings, "ENABLED", True),
    ):
        return await create_daiv_agent(
            settings=settings,
            ctx=ctx,
            workspace=FakeWorkspace(bash=MagicMock(), session=MagicMock()),
            artifact_store=FakeArtifactStore(),
            checkpointer=checkpointer,
        )


_RESET_CHANNELS = {
    "_blob_payloads",
    "_skill_tools_disclosed",
    "_summarization_session_id",
    "active_skill_mode",
    "active_skill_model",
    "code_changes",
    "github_token",
    "github_token_expires_at",
    "jump_to",
    "memory_contents",
    "model_patch",
    "pinned_skills",
    "pre_run_dirty_files",
    "protected_branch_fallback_source",
    "published",
    "sandbox_fingerprint",
    "session_id",
    "skills_load_errors",
    "skills_metadata",
    "structured_response",
}


@pytest.mark.django_db(transaction=True)
async def test_every_channel_of_the_agents_state_is_archived_or_resets_on_restore():
    """A channel the agent's state gains must be sorted into ``ARCHIVED_CHANNELS`` or ``_RESET_CHANNELS``."""
    agent = await _daiv_agent()
    channels = {name for name in agent.channels if not name.startswith(("__", "branch:"))}

    assert set(ARCHIVED_CHANNELS) <= channels
    assert channels - set(ARCHIVED_CHANNELS) == _RESET_CHANNELS


def _stub_agent(error: Exception | None = None) -> SimpleNamespace:
    return SimpleNamespace(aupdate_state=AsyncMock(side_effect=error))


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


async def _aresume(agent, saver: InMemorySaver, config: dict) -> None:
    """Restore the thread, then run a follow-up turn on it."""
    assert await arestore_thread(agent, saver, config) is True
    await agent.ainvoke({"messages": [HumanMessage(content="follow-up", id="h2")]}, config)


@pytest.mark.django_db(transaction=True)
class TestRestoreThread:
    async def test_an_expired_thread_is_seeded_from_its_latest_context(self):
        thread_id = await amake_job_session()
        await acreate_archive(thread_id, 0, _turns("h1", "a1"), closed=True)
        await acreate_archive(thread_id, 1, [AIMessage("cleared", id="r1")])
        agent, config = _stub_agent(), _config(thread_id)

        assert await arestore_thread(agent, fake_checkpointer(None), config) is True

        args, kwargs = agent.aupdate_state.await_args
        assert args[0] is config
        assert _ids(args[1]["messages"]) == ["r1"]
        assert kwargs == {"as_node": "model"}

    @pytest.mark.parametrize(
        ("contexts", "live"),
        [
            pytest.param([(_turns("h1", "a1"), True)], None, id="latest-context-cleared"),
            pytest.param([(_turns("h1"), False)], {"messages": []}, id="live-checkpoint"),
            pytest.param([], None, id="no-archive"),
        ],
    )
    async def test_a_thread_is_left_alone(self, contexts, live):
        thread_id = await amake_job_session()
        for epoch, (messages, closed) in enumerate(contexts):
            await acreate_archive(thread_id, epoch, messages, closed=closed)
        agent = _stub_agent()

        assert await arestore_thread(agent, fake_checkpointer(live), _config(thread_id)) is False
        agent.aupdate_state.assert_not_awaited()

    async def test_the_merge_request_is_restored(self):
        thread_id = await amake_job_session()
        await acreate_archive(thread_id, 0, _turns("h1"), merge_request=make_merge_request())
        agent = _stub_agent()

        await arestore_thread(agent, fake_checkpointer(None), _config(thread_id))

        assert agent.aupdate_state.await_args.args[1]["merge_request"] == make_merge_request()

    async def test_a_merge_request_that_no_longer_revives_is_dropped(self):
        thread_id = await amake_job_session()
        await acreate_archive(thread_id, 0, _turns("h1"), merge_request={"repo_id": "group/repo"})
        agent = _stub_agent()

        await arestore_thread(agent, fake_checkpointer(None), _config(thread_id))

        assert "merge_request" not in agent.aupdate_state.await_args.args[1]

    async def test_a_context_that_fails_to_restore_fails_the_run(self):
        thread_id = await amake_job_session()
        await acreate_archive(thread_id, 0, _turns("h1"))

        with pytest.raises(ArchiveRestoreError, match=r"context 0") as raised:
            await arestore_thread(_stub_agent(ValueError("bad update")), fake_checkpointer(None), _config(thread_id))

        assert isinstance(raised.value.__cause__, ValueError)

    async def test_a_restored_thread_resumes_with_its_history_and_private_state(self):
        thread_id = await amake_job_session()
        await acreate_archive(thread_id, 0, _turns("h1", "a1"), loaded_tool_names={"web_fetch"})
        probe, saver = _Probe(), InMemorySaver()

        await _aresume(_deep_agent(probe, saver), saver, _config(thread_id))

        assert probe.seen == [(["h1", "a1", "h2"], {"web_fetch"})]

    async def test_the_daiv_agent_restores_every_archived_channel(self):
        thread_id = await amake_job_session()
        values = archivable_values(_state())
        await acreate_archive(thread_id, 0, **values)
        saver, config = InMemorySaver(), _config(thread_id)
        agent = await _daiv_agent(saver)

        assert await arestore_thread(agent, saver, config) is True
        assert archivable_values((await agent.aget_state(config)).values) == values

    async def test_a_dangling_tool_call_in_the_archive_is_not_replayed(self):
        calls: list[str] = []

        @tool
        def ping() -> str:
            """Ping."""
            calls.append("ping")
            return "pong"

        thread_id = await amake_job_session()
        dangling = AIMessage(content="", id="a1", tool_calls=[{"id": "c1", "name": "ping", "args": {}}])
        await acreate_archive(thread_id, 0, [_turns("h1")[0], dangling])
        saver = InMemorySaver()

        await _aresume(_deep_agent(_Probe(), saver, tools=(ping,)), saver, _config(thread_id))

        assert calls == []

    async def test_a_thread_read_through_the_parent_chain_walk_archives_and_resumes(self):
        thread_id = await amake_job_session()
        config = _config(thread_id)
        first = _ParentChainSaver()
        await _deep_agent(_Probe(), first, reply_id="a1").ainvoke(
            {"messages": [HumanMessage(content="first", id="h1")]}, config
        )

        assert "messages" not in (await first.aget_tuple(config)).checkpoint["channel_values"]
        assert await aarchive_thread(first, thread_id) is True
        assert await _contexts(thread_id) == [(0, ["h1", "a1"], False)]

        probe, fresh = _Probe(), _ParentChainSaver()
        await _aresume(_deep_agent(probe, fresh), fresh, config)

        assert [ids for ids, _ in probe.seen] == [["h1", "a1", "h2"]]
        assert await aarchive_thread(fresh, thread_id) is True
        assert await _contexts(thread_id) == [(0, ["h1", "a1", "h2", "a2"], False)]

    async def test_a_clear_through_the_parent_chain_walk_keeps_the_earlier_context_out_of_the_next_run(self):
        """``/clear`` deletes the thread mid-run, as ``ClearSlashCommand`` does, and answers with a reset."""
        thread_id = await amake_job_session()
        config = _config(thread_id)
        saver = _ParentChainSaver()
        issue = HumanMessage(content="the issue", id="x")

        async def _run(prompt: HumanMessage, reply_id: str, *, reset: bool = False, probe=None):
            agent = _deep_agent(probe or _Probe(), saver, reply_id=reply_id, middleware=(_Clear(saver, thread_id),))
            await agent.ainvoke({"messages": [issue, prompt]}, config)
            await aarchive_thread(saver, thread_id, reset=reset)

        await _run(HumanMessage(content="fix it", id="h1"), "a1")
        await _run(HumanMessage(content="/clear", id="h2"), "unused", reset=True)
        await _run(HumanMessage(content="start over", id="h3"), "a3")

        assert await _contexts(thread_id) == [(0, ["x", "h1", "a1"], True), (1, ["x", "h3", "a3"], False)]
        probe, fresh = _Probe(), _ParentChainSaver()
        agent = _deep_agent(probe, fresh, reply_id="a4")
        assert await arestore_thread(agent, fresh, config) is True
        await agent.ainvoke({"messages": [issue, HumanMessage(content="and then", id="h4")]}, config)
        assert [ids for ids, _ in probe.seen] == [["x", "h3", "a3", "h4"]]
