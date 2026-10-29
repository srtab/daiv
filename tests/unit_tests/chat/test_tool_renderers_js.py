"""Behavioral tests for ``toolRow`` in ``tool-renderers.js``, under node."""

from __future__ import annotations

import json

import pytest

from tests.unit_tests.chat.chat_stream_driver import CHAT_STREAM_JS
from tests.unit_tests.jsdriver import requires_node, run_node

pytestmark = requires_node

JS_DIR = CHAT_STREAM_JS.parent

_HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const payload = JSON.parse(readFileSync(0, "utf8"));
const countLines = (s) => (s ? s.split("\\n").length - (s.endsWith("\\n") ? 1 : 0) : 0);
const sandbox = {
  document: { getElementById: () => null },
  Diff: {
    diffLines: (a, b) => [
      { removed: true, count: countLines(a) },
      { added: true, count: countLines(b) },
    ],
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
for (const src of payload.sources) vm.runInContext(readFileSync(src, "utf8"), sandbox);
sandbox.__seg = payload.seg;
sandbox.__ctx = payload.ctx;
process.stdout.write(JSON.stringify(vm.runInContext("window.toolRow(__seg, __ctx)", sandbox)));
"""


def _row_seg(seg: dict, servers: list[str] | None = None) -> dict:
    sources = [str(JS_DIR / "transcript-labels.js"), str(JS_DIR / "tool-renderers.js")]
    return run_node(_HARNESS, {"sources": sources, "seg": seg, "ctx": {"mcpServers": servers or []}})


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
    assert row["category"] == {"key": "edit"}


def test_bash_non_zero_exit_is_failed():
    result = json.dumps({"commands": [{"command": "pytest", "exit_code": 1, "output": ""}], "files_changed": []})
    row = _row("bash", {"command": "pytest"}, result=result)
    assert row["failed"] is True and row["phrase"] == "Ran pytest"
    assert row["detail"] == "exit 1"


def test_bash_zero_exit_is_not_failed():
    result = json.dumps({"commands": [{"command": "ls", "exit_code": 0, "output": ""}], "files_changed": []})
    row = _row("bash", {"command": "ls"}, result=result)
    assert row["failed"] is False and row["detail"] == ""


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
