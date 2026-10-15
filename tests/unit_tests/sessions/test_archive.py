from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from redis.exceptions import RedisError
from sessions.archive import (
    ARCHIVED_CHANNELS,
    aarchive_thread,
    aarchived_contexts,
    aload_archived_values,
    archivable_values,
    continues_context,
    decode_values,
    encode_values,
)

from codebase.base import MergeRequest, User
from tests.unit_tests.sessions.conftest import amake_job_session


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


def test_a_context_continues_while_its_first_message_is_there():
    assert continues_context("h1", _turns("h1", "a1")) is True
    assert continues_context("h1", _turns("r1")) is False
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

        with caplog.at_level("WARNING", logger="daiv.sessions"):
            assert await aarchive_thread(_checkpointer({"messages": _turns("h1", "a1")}), thread_id) is False
            assert await aarchive_thread(_checkpointer({"messages": _turns("h2", "a2")}), thread_id) is False

        [context] = await aarchived_contexts(thread_id)
        assert context.message_count == 4
        assert "keeping the archive" in caplog.text

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
