"""Behavioral tests for ``toolRow`` in ``tool-renderers.js``, under node."""

from __future__ import annotations

import json

import pytest

from tests.unit_tests.chat.transcript_driver import run_transcript
from tests.unit_tests.jsdriver import requires_node

pytestmark = requires_node


def _row_seg(seg: dict, servers: list[str] | None = None) -> dict:
    values = {"seg": seg, "ctx": {"mcpServers": servers or []}}
    return run_transcript("toolRow(vars.seg, vars.ctx)", values, blocks=False)


def _row(name: str, args: dict, *, result: str | None = None, status: str = "done", servers=None) -> dict:
    seg = {"type": "tool_call", "id": "c1", "name": name, "args": json.dumps(args), "result": result, "status": status}
    return _row_seg(seg, servers)


def test_read_file_row():
    row = _row("read_file", {"file_path": "/workspace/repo/daiv/sessions/queue.py", "offset": 1, "limit": 180})
    assert (row["phrase"], row["livePhrase"], row["icon"]) == ("Read queue.py", "Reading queue.py", "document-text")
    assert row["detail"] == "daiv/sessions · 1–180"
    assert row["badges"] == []
    assert row["category"] == {"key": "read"}


def test_edit_row_carries_line_counts_not_badges():
    row = _row("edit_file", {"file_path": "/workspace/repo/a/b.py", "old_string": "x\n", "new_string": "y\nz\n"})
    assert row["diff"] == {"added": 2, "removed": 1}
    assert row["phrase"] == "Edited b.py"
    assert all(b["text"][0] not in "+−" for b in row["badges"])


def test_write_file_row_counts_content_lines():
    row = _row("write_file", {"file_path": "/workspace/repo/a/new.py", "content": "a\nb\nc\n"})
    assert (row["phrase"], row["icon"], row["detail"]) == ("Created new.py", "document-plus", "a")
    assert row["diff"] == {"added": 3, "removed": 0}
    assert row["category"] == {"key": "create"}


def test_bash_non_zero_exit_is_failed():
    result = json.dumps({"commands": [{"command": "pytest", "exit_code": 1, "output": ""}], "files_changed": []})
    row = _row("bash", {"command": "pytest"}, result=result)
    assert row["failed"] is True and row["phrase"] == "Ran pytest"
    assert row["detail"] == "exit 1"


def test_bash_zero_exit_is_not_failed():
    result = json.dumps({"commands": [{"command": "ls", "exit_code": 0, "output": ""}], "files_changed": []})
    row = _row("bash", {"command": "ls"}, result=result)
    assert row["failed"] is False and row["detail"] == "passed"


def test_bash_detail_and_failed_come_from_the_same_structured_result():
    free_text = _row("bash", {"command": "x"}, result="exit 2")
    assert (free_text["failed"], free_text["detail"]) == (False, "")
    no_code = json.dumps({"commands": [{"command": "x", "output": ""}], "files_changed": []})
    row = _row("bash", {"command": "x"}, result=no_code)
    assert (row["failed"], row["detail"]) == (False, "passed")


def test_bash_without_result_has_no_detail():
    assert _row("bash", {"command": "x"}, status="running")["detail"] == ""


def test_legacy_edit_with_a_unified_diff_result_still_has_counts():
    patch = "--- a/f\n+++ b/f\n@@ -1,2 +1,3 @@\n-old\n+new\n+more\n ctx\n"
    row = _row("edit_file", {"file_path": "/workspace/repo/f.py"}, result=patch)
    assert row["diff"] == {"added": 2, "removed": 1}


@pytest.mark.parametrize("result,status", [("error: boom", "done"), (None, "error")])
def test_error_result_or_unanswered_call_is_failed(result, status):
    assert _row("grep", {"pattern": "x"}, result=result, status=status)["failed"] is True


def test_delegate_jobs_result_is_not_judged_by_row():
    assert _row("delegate_jobs", {}, result="error: nope")["failed"] is False


def test_grep_scope_goes_to_detail_and_not_badges():
    row = _row("grep", {"pattern": "foo", "path": "/workspace/repo/daiv/chat"}, result="a.py\nb.py")
    assert row["phrase"] == 'Searched for "foo"'
    assert row["detail"] == "daiv/chat"
    assert row["badges"] == [{"text": "2 files", "tone": "warn"}]
    assert row["category"] == {"key": "search"}


def test_mcp_tool_splits_on_the_longest_known_server_name():
    row = _row("sentry_dev_search_issues", {"query": "x"}, servers=["sentry", "sentry_dev"])
    assert row["phrase"] == "sentry_dev · search issues"
    assert row["category"] == {"key": "mcp", "name": "sentry_dev"}
    assert row["detail"] == "x"


def test_unknown_tool_without_a_matching_server_keeps_its_raw_name():
    row = _row("some_new_tool", {"a": 1})
    assert row["phrase"] == "some_new_tool"
    assert (row["icon"], row["category"]) == ("puzzle-piece", {"key": "other"})


def test_platform_rows_use_brand_names():
    row = _row("gitlab", {"subcommand": "mr view --iid 5"})
    assert (row["phrase"], row["livePhrase"], row["icon"]) == ("GitLab", "GitLab", "gitlab")
    assert row["detail"] == "mr view --iid 5"
    assert row["category"] == {"key": "platform", "name": "GitLab"}


@pytest.mark.parametrize(
    "name,phrase,icon,category",
    [
        ("publish_artifact", "Published", "paper-clip", {"key": "artifact"}),
        ("delegate_jobs", "Delegated jobs", "share", {"key": "jobs"}),
        ("check_delegated_jobs", "Checked delegated jobs", "share", {"key": "jobs"}),
        ("cancel_delegated_jobs", "Cancelled delegated jobs", "share", {"key": "jobs"}),
    ],
)
def test_special_calls_have_rows(name, phrase, icon, category):
    row = _row(name, {"batch_id": "b1", "path": "/workspace/tmp/a.md"})
    assert (row["phrase"], row["livePhrase"], row["icon"], row["category"]) == (phrase, phrase, icon, category)


def test_batch_id_is_the_detail_of_check_rows():
    assert _row("check_delegated_jobs", {"batch_id": "b1"})["detail"] == "b1"


def test_running_thinking_row_reads_thinking():
    row = _row_seg({"type": "thinking", "content": "", "status": "running"})
    assert row["livePhrase"] == "Thinking…"
    assert (row["icon"], row["category"]) == ("light-bulb", {"key": "thinking"})


@pytest.mark.parametrize("extra,phrase", [({"startedAt": 1000, "endedAt": 5400}, "Thought for 4s"), ({}, "Thought")])
def test_finished_thinking_row_shows_duration_only_when_timed(extra, phrase):
    assert _row_seg({"type": "thinking", "content": "x", "status": "done", **extra})["phrase"] == phrase
