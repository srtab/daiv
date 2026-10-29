"""Behavioral tests for ``toolRow`` and ``toolBodyHTML`` in ``tool-renderers.js``, under node."""

from __future__ import annotations

import json
import re

import pytest

from tests.unit_tests.chat.transcript_driver import run_transcript
from tests.unit_tests.chat.transcript_fixtures import tool_call
from tests.unit_tests.jsdriver import requires_node

pytestmark = requires_node


def _row_seg(seg: dict, servers: list[str] | None = None) -> dict:
    values = {"seg": seg, "ctx": {"mcpServers": servers or []}}
    return run_transcript("toolRow(vars.seg, vars.ctx)", values, blocks=False)


def _row(name: str, args: dict, *, result: str | None = None, status: str = "done", servers=None) -> dict:
    return _row_seg(tool_call("c1", name, args, result=result, status=status), servers)


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
    "name,phrase,live,icon,category",
    [
        ("publish_artifact", "Published", "Publishing", "paper-clip", {"key": "artifact"}),
        ("delegate_jobs", "Delegated jobs", "Delegating jobs", "share", {"key": "jobs"}),
        ("check_delegated_jobs", "Checked delegated jobs", "Checking delegated jobs", "share", {"key": "jobs"}),
        ("cancel_delegated_jobs", "Cancelled delegated jobs", "Cancelling delegated jobs", "share", {"key": "jobs"}),
    ],
)
def test_special_calls_have_rows(name, phrase, live, icon, category):
    row = _row(name, {"batch_id": "b1", "path": "/workspace/tmp/a.md"})
    assert (row["phrase"], row["livePhrase"], row["icon"], row["category"]) == (phrase, live, icon, category)


def test_the_repo_root_reads_as_dot_and_drops_out_of_a_search_scope():
    assert _row("ls", {"path": "/workspace/repo"})["phrase"] == "Listed ."
    assert _row("grep", {"pattern": "foo", "path": "/workspace/repo", "glob": "*.py"})["detail"] == "*.py"
    assert _row("ls", {"path": "/workspace/repository"})["phrase"] == "Listed /workspace/repository"


def test_question_row_shows_the_question_while_it_streams_and_when_refused():
    args = json.dumps({"questions": [{"header": "Approach", "question": "Which fix?", "options": []}]})
    seg = {"type": "tool_call", "id": "q1", "name": "ask_user_question", "result": None, "status": "running"}
    live = _row_seg({**seg, "args": args[: args.index("fix") + 3]})
    assert (live["livePhrase"], live["icon"], live["detail"]) == ("Asking a question", "chat-bubble", "Which fix")
    refused = _row_seg({**seg, "args": args, "result": "error: ask alone", "status": "error"})
    assert (refused["phrase"], refused["detail"], refused["failed"]) == ("Asked a question", "Which fix?", True)
    assert refused["category"] == {"key": "question"}


def test_batch_id_is_the_detail_of_check_rows():
    assert _row("check_delegated_jobs", {"batch_id": "b1"})["detail"] == "b1"


def test_running_thinking_row_reads_thinking():
    row = _row_seg({"type": "thinking", "content": "", "status": "running"})
    assert row["livePhrase"] == "Thinking…"
    assert (row["icon"], row["category"]) == ("light-bulb", {"key": "thinking"})


@pytest.mark.parametrize("extra,phrase", [({"startedAt": 1000, "endedAt": 5400}, "Thought for 4s"), ({}, "Thought")])
def test_finished_thinking_row_shows_duration_only_when_timed(extra, phrase):
    assert _row_seg({"type": "thinking", "content": "x", "status": "done", **extra})["phrase"] == phrase


def _body_seg(seg: dict, *, show_raw: bool = False, servers: list[dict] | None = None, mr_url: str | None = None):
    rows = servers or []
    ctx = {"mcpServers": [row["name"] for row in rows], "mcpRows": rows, "mergeRequestUrl": mr_url}
    expr = "toolBodyHTML(vars.seg, { ...vars.ctx, latestArtifactsById: new Map() })"
    return run_transcript(expr, {"seg": seg, "ctx": ctx}, blocks=False, labels={"show_raw": show_raw}, markdown=True)


def _body(
    name: str,
    args: dict,
    *,
    result: str | None = None,
    status: str = "done",
    show_raw: bool = False,
    servers: list[dict] | None = None,
    mr_url: str | None = None,
    **seg_extra,
) -> str:
    seg = {**tool_call("c1", name, args, result=result, status=status), **seg_extra}
    return _body_seg(seg, show_raw=show_raw, servers=servers, mr_url=mr_url)


EVICTED = (
    "Tool result too large, the result of this tool call c1 was saved in the filesystem at this path: "
    "/large_tool_results/c1\n\npreview line"
)


def test_default_body_turns_a_json_list_into_a_table():
    html = _body("some_tool", {"a": 1}, result=json.dumps([{"id": 1, "title": "x"}, {"id": 2, "title": "y"}]))
    assert "<table" in html and ">title<" in html and "Arguments" not in html


def test_raw_only_in_debug_mode():
    assert "Arguments" in _body("some_tool", {"a": 1}, result="ok", show_raw=True)


def test_unparseable_json_falls_back_to_markdown():
    assert "<table" not in _body("some_tool", {}, result="[not json")


def test_offloaded_result_says_only_a_preview_was_loaded():
    result = EVICTED
    html = _body("bash", {"command": "x"}, result=result)
    assert "/large_tool_results/c1" in html and "saw only a preview" in html


def test_huge_result_is_clamped():
    html = _body("some_tool", {}, result="\n".join(f"line {i}" for i in range(5000)))
    assert html.count("line ") < 5000 and "Show all" in html


def test_failed_call_leads_with_what_went_wrong():
    assert _body("grep", {"pattern": "x"}, result="error: invalid regex").startswith('<div class="body-error"')


def test_an_error_is_said_once():
    assert _body("some_tool", {}, result="error: invalid regex").count("invalid regex") == 1


def test_a_call_the_run_never_answered_says_so():
    html = _body("grep", {"pattern": "x"}, status="error")
    assert html.startswith('<div class="body-error"')
    assert "No result: the run ended before this call returned." in html


def test_raw_is_a_closed_details_after_the_other_sections():
    html = _body("some_tool", {"a": 1}, result="ok", show_raw=True)
    assert html.index("Loaded into context") < html.index('<details class="body-raw"><summary>Raw</summary>')
    assert html.endswith("</details>") and '{\n  "a": 1\n}' in html


@pytest.mark.parametrize(
    "result,size",
    [("a\nb", "2 lines · 0.1 KB"), ("x" * 3000, "1 line · 2.9 KB"), ("y\n" * 6000, "6000 lines · 12 KB")],
    ids=["tiny", "one-long-line", "many-lines"],
)
def test_context_carries_its_size_in_lines_and_kb(result, size):
    assert f'<span class="body-size">{size}</span>' in _body("some_tool", {}, result=result)


def test_clamp_shows_twelve_lines_and_caps_the_rest():
    html = _body("read_file", {"file_path": "/workspace/repo/a.py"}, result="\n".join(f"row {i}" for i in range(5000)))
    visible, more = html.split('<details class="body-more">')
    assert visible.count("row ") == 12 and more.count("row ") == 488
    assert "Show all 5000 lines" in more and "and 4500 more" in more


def test_short_result_is_not_clamped():
    assert "Show all" not in _body("read_file", {"file_path": "/workspace/repo/a.py"}, result="1  a\n2  b")


def test_long_markdown_is_cut_at_a_blank_line():
    result = "\n".join([f"p{i}" for i in range(14)] + ["", "tail"])
    visible, more = _body("some_tool", {}, result=result).split('<details class="body-more">')
    assert "p13" in visible and "tail" in more


def test_long_markdown_cut_inside_a_code_fence_closes_and_reopens_it():
    lines = [f"para {i}" for i in range(10)] + ["```python"] + [f"code {i}" for i in range(20)] + ["```"]
    visible, more = _body("some_tool", {}, result="\n".join(lines)).split('<details class="body-more">')
    assert visible.count("```") == 2 and more.startswith("<summary>") and more.count("```python") == 1


def test_offloaded_file_write_names_the_full_size():
    result = "Wrote 20480 bytes (400 lines) to /workspace/tmp/large_tool_results/c1\nPreview (first 1 lines):\nid: 1"
    html = _body("gitlab", {"subcommand": "project-job trace --id 3"}, result=result)
    assert "saw only a preview" in html and "/workspace/tmp/large_tool_results/c1" in html
    assert "400 lines · 20 KB" in html and "id: 1" in html


@pytest.mark.parametrize("name", ["read_file", "grep", "bash", "task", "web_fetch", "gitlab", "sentry_search"])
def test_every_tool_shows_the_offload_notice(name):
    html = _body(name, {"file_path": "/workspace/repo/a.py", "url": "https://x", "prompt": "p"}, result=EVICTED)
    assert "saw only a preview" in html and "preview line" in html


def test_json_object_becomes_a_field_list():
    html = _body("some_tool", {}, result=json.dumps({"state": "opened", "labels": ["a"]}))
    assert '<dl class="body-fields">' in html and "<dt>state</dt><dd>opened</dd>" in html and '["a"]' in html


def test_table_keeps_six_columns_and_fifty_rows():
    html = _body("some_tool", {}, result=json.dumps([{f"k{i}": i for i in range(8)} for _ in range(60)]))
    assert html.count("<th>") == 6 and html.count("<tr>") == 51 and "and 10 more" in html


def test_untrusted_result_text_is_escaped():
    assert "<img" not in _body("some_tool", {}, result='[{"name": "<img src=x onerror=alert(1)>"}]')
    assert "<script>" not in _body("some_tool", {}, result="<script>alert(1)</script>")


@pytest.mark.parametrize(
    "name,args",
    [("edit_file", {"file_path": "/workspace/repo/a.py"}), ("skill", {"skill": "x"}), ("web_search", {"query": "q"})],
)
def test_legacy_bodies_no_longer_dump_the_arguments(name, args):
    assert "Arguments" not in _body(name, args)


def test_a_link_from_a_result_cannot_break_out_of_its_attribute():
    result = json.dumps([{"title": "t", "link": 'https://x" onmouseover="alert(1)', "content": ""}])
    html = _body("web_search", {"query": "q"}, result=result)
    assert re.search(r'<a [^>]*\sonmouseover="', html) is None


def test_a_non_web_link_from_a_result_is_not_a_link():
    result = json.dumps([{"title": "t", "link": "javascript:alert(1)", "content": ""}])
    assert 'href="javascript:' not in _body("web_search", {"query": "q"}, result=result)


def test_text_result_renders_as_markdown_or_escaped_text_without_a_renderer():
    seg = tool_call("c1", "some_tool", {}, result="**hi** <b>")
    assert "<strong>hi</strong> &lt;b&gt;" in _body_seg(seg)
    bare = run_transcript("toolBodyHTML(vars.seg, {})", {"seg": seg}, blocks=False)
    assert '<pre class="body-pre">**hi** &lt;b&gt;</pre>' in bare
