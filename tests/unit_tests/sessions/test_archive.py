from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sessions.archive import ARCHIVED_CHANNELS, archivable_values, decode_values, encode_values

from codebase.base import MergeRequest, User


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
