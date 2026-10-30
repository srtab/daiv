"""Behavioral tests for ``toolRow`` and ``toolBodyHTML`` in ``tool-renderers.js``, under node."""

from __future__ import annotations

import json
import re
import time

import pytest

from tests.unit_tests.chat.transcript_driver import run_transcript
from tests.unit_tests.chat.transcript_fixtures import tool_call
from tests.unit_tests.jsdriver import requires_node

pytestmark = requires_node


def _row_seg(seg: dict, servers: list[str] | None = None) -> dict:
    values = {"seg": seg, "ctx": {"mcpRows": [{"name": name} for name in servers or []]}}
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


def test_delegate_jobs_row_fails_on_an_error_text_as_its_body_says():
    assert _row("delegate_jobs", {}, result="error: nope")["failed"] is True
    assert "What went wrong" in _body("delegate_jobs", {}, result="error: nope")


@pytest.mark.parametrize(
    "result,failed",
    [
        ({"error": "rate limited"}, True),
        ({"batch_id": "b", "started": [], "failed": [{"repo_id": "a/x", "error": "denied"}]}, True),
        ({"batch_id": "b", "started": [{"repo_id": "a/x", "thread_id": "t"}], "failed": []}, False),
    ],
)
def test_delegate_jobs_row_fails_when_the_call_errored_or_started_no_leg(result, failed):
    assert _row("delegate_jobs", {}, result=json.dumps(result))["failed"] is failed


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
        (
            "cancel_delegated_jobs",
            "Cancelled delegated jobs",
            "Cancelling delegated jobs",
            "share",
            {"key": "cancel_jobs"},
        ),
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


SESSION_PATH = "/dashboard/sessions/{thread_id}/"


def _body_seg(seg: dict, *, show_raw: bool = False, servers: list[dict] | None = None, mr_url: str | None = None):
    rows = servers or []
    ctx = {"mcpRows": rows, "mergeRequestUrl": mr_url}
    expr = "toolBodyHTML(vars.seg, { ...vars.ctx, latestArtifactsById: new Map() })"
    labels = {"show_raw": show_raw, "session_path": SESSION_PATH}
    return run_transcript(expr, {"seg": seg, "ctx": ctx}, blocks=False, labels=labels, markdown=True)


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
    assert "Show all 500 lines" in more and "and 4500 more" in more


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


@pytest.mark.parametrize("name", ["read_file", "some_tool"])
def test_an_over_long_line_is_cut_and_the_lines_after_it_stay(name):
    html = _body(name, {}, result="x" * 60000 + "\nDone, exit 0\nthird")
    assert "Done, exit 0" in html and "third" in html and "body-and-more" not in html
    assert "x" * 2000 + "…" in html and "x" * 2001 not in html


@pytest.mark.parametrize("name", ["read_file", "some_tool"])
def test_show_all_holds_the_lines_it_counts(name):
    result = "\n".join(["x" * 60000] + [f"row {i}" for i in range(1, 20)])
    visible, more = _body(name, {}, result=result).split('<details class="body-more">')
    assert visible.count("row ") == 11 and more.count("row ") == 8
    assert "Show all 20 lines" in more and "body-and-more" not in more


@pytest.mark.parametrize("name", ["read_file", "some_tool"])
def test_lines_past_the_size_cap_are_counted_not_lost(name):
    html = _body(name, {}, result="\n".join(["y" * 2500] * 100))
    kept = html.count("y" * 2000 + "…")
    assert 12 < kept < 100
    assert f"Show all {kept} lines" in html and f"and {100 - kept} more" in html


def _body_with(seg: dict, setup: str, *, labels: dict | None = None) -> str:
    return run_transcript(f"({setup}, toolBodyHTML(vars.seg, {{}}))", {"seg": seg}, blocks=False, labels=labels or {})


FAKE_HLJS = """window.hljs = {
  getLanguage: (lang) => (lang === "py" ? { name: "python" } : undefined),
  highlight: (code) => {
    const escaped = code.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
    return { value: `<span class="hljs-string">${escaped}</span>` };
  },
}"""

ICONS = {"document-text": '<svg data-icon="doc"></svg>', "folder": '<svg data-icon="folder"></svg>'}


def test_read_file_keeps_the_files_line_numbers():
    html = _body("read_file", {"file_path": "/workspace/repo/a.py"}, result="86  def f():\n87      return 1")
    assert '<span class="ln">86</span>' in html and '<span class="ln">87</span>' in html


def test_read_file_numbers_its_lines_from_the_read_header():
    result = "@@ lines 86-87 of 412 | next offset 88 @@\ndef f():\n    return 1"
    html = _body("read_file", {"file_path": "/workspace/repo/a.py", "offset": 85, "limit": 2}, result=result)
    assert '<span class="ln">86</span>def f():' in html and '<span class="ln">87</span>    return 1' in html
    assert "@@" not in html


def test_read_file_keeps_continuation_markers_and_the_legacy_tab_gutter():
    html = _body("read_file", {"file_path": "/workspace/repo/a.txt"}, result="  5  aaa\n5.1  bbb\n  6  ")
    assert '<span class="ln">5</span>aaa' in html and '<span class="ln">5.1</span>bbb' in html
    assert '<span class="ln">6</span>' in html
    legacy = _body("read_file", {"file_path": "/workspace/repo/a.txt"}, result="     1\tfrom x import y\n     2\t\tz")
    assert '<span class="ln">1</span>from x import y' in legacy and '<span class="ln">2</span>\tz' in legacy


def test_read_file_highlights_by_extension_and_keeps_each_line_whole():
    seg = tool_call(
        "c1", "read_file", {"file_path": "/workspace/repo/a.py"}, result='@@ lines 1-2 @@\ns = """a\nb <c>"""'
    )
    html = _body_with(seg, FAKE_HLJS)
    assert '<span class="ln">1</span><span class="hljs-string">s = """a</span>' in html
    assert '<span class="ln">2</span><span class="hljs-string">b &lt;c&gt;"""</span>' in html


def test_read_file_in_an_unknown_language_stays_plain():
    seg = tool_call("c1", "read_file", {"file_path": "/workspace/repo/a.xyz"}, result="1  <b>")
    html = _body_with(seg, FAKE_HLJS)
    assert "hljs-" not in html and '<span class="ln">1</span>&lt;b&gt;' in html


def test_read_file_clamps_at_twelve_lines():
    result = "\n".join(f"{i}  line {i}" for i in range(1, 31))
    html = _body("read_file", {"file_path": "/workspace/repo/a.py"}, result=result)
    visible, more = html.split('<details class="body-more">')
    assert visible.count('class="ln"') == 12 and more.count('class="ln"') == 18 and "Show all 30 lines" in more


def test_read_file_keeps_its_notices_and_numbers_no_line_past_its_window():
    notice = "[Requested offset -1 is before the start of the file; read from line 1 instead.]"
    html = _body(
        "read_file", {"file_path": "/workspace/repo/a.txt"}, result=f"{notice}\n@@ lines 1-1 of 1 @@\nx\n\n[banner]"
    )
    assert "Requested offset -1" in html and '<span class="ln">1</span>x' in html
    assert "[banner]" in html and 'class="ln">2<' not in html


def test_edit_diff_has_no_line_numbers():
    html = _body(
        "edit_file",
        {"file_path": "/workspace/repo/a.py", "old_string": "a\n", "new_string": "b\n"},
        result="Successfully replaced 1 instance",
    )
    assert "@@" not in html and 'class="ln"' not in html


def test_edit_shows_the_removed_and_added_lines_then_the_reply():
    html = _body(
        "edit_file",
        {"file_path": "/workspace/repo/a.py", "old_string": "a\n", "new_string": "<b>\n"},
        result="Successfully replaced 1 instance",
    )
    changed, context = html.split("Loaded into context")
    assert "Changed" in changed and '<div class="diff-del">-a</div><div class="diff-add">+&lt;b&gt;</div>' in changed
    assert "Successfully replaced 1 instance" in context


def test_edit_diff_keeps_two_lines_of_context_around_each_change():
    stub = r"""window.Diff = { diffLines: () => [
      { value: "c1\nc2\nc3\nc4\n" }, { removed: true, value: "x\n" }, { added: true, value: "y\n" },
      { value: "c5\nc6\nc7\nc8\nc9\n" }, { removed: true, value: "z\n" },
    ] }"""
    args = {"file_path": "/workspace/repo/a.py", "old_string": "o", "new_string": "n"}
    html = _body_with(tool_call("c1", "edit_file", args, result="Successfully replaced 1 instance"), stub)
    assert all(f"c{i}" in html for i in (3, 4, 5, 6, 8, 9)) and not any(f"c{i}" in html for i in (1, 2, 7))
    assert html.count('class="diff-gap"') == 1


EDIT_ARGS = {"file_path": "/workspace/repo/a.py", "old_string": "a\n", "new_string": "b\n"}
WRITE_ARGS = {"file_path": "/workspace/repo/n.py", "content": "a\n"}


@pytest.mark.parametrize("name,args", [("edit_file", EDIT_ARGS), ("write_file", WRITE_ARGS)])
@pytest.mark.parametrize(
    "result,status",
    [(None, "error"), (None, "running"), ("Error: file not found", "done")],
    ids=["unanswered", "running", "error"],
)
def test_nothing_changed_until_the_write_succeeded(name, args, result, status):
    assert "Changed" not in _body(name, args, result=result, status=status)


@pytest.mark.parametrize(
    "name,args,result",
    [("edit_file", EDIT_ARGS, "Successfully replaced 1 instance"), ("write_file", WRITE_ARGS, "Updated file n.py")],
)
def test_a_successful_write_shows_what_changed(name, args, result):
    assert "Changed" in _body(name, args, result=result)


def test_write_file_changed_is_the_new_content_without_line_numbers():
    args = {"file_path": "/workspace/repo/n.py", "content": "a\n<b>\n"}
    html = _body("write_file", args, result="Updated file /workspace/repo/n.py")
    assert '<div class="diff-add">+a</div><div class="diff-add">+&lt;b&gt;</div>' in html and 'class="ln"' not in html


def test_grep_invalid_js_regex_still_renders():
    html = _body("grep", {"pattern": "(?i)foo", "output_mode": "content"}, result="a.py:\n  3: Foo")
    assert "Foo" in html and "<mark>" not in html


def test_grep_marks_the_pattern_and_leaves_the_count_to_the_row():
    result = "Mode: content — matching lines.\n\na.py:\n  3: foo <b> foo\nb.py:\n  9: foo"
    html = _body("grep", {"pattern": "foo", "output_mode": "content"}, result=result)
    assert html.count("<mark>foo</mark>") == 3 and "&lt;b&gt;" in html
    assert '<span class="body-size">6 lines · 0.1 KB</span>' in html and "matches" not in html


def test_grep_highlight_never_cuts_an_escaped_character():
    html = _body("grep", {"pattern": "lt|amp", "output_mode": "content"}, result="a.py:\n  1: a<b & lt")
    assert "a&lt;b &amp; <mark>lt</mark>" in html


def test_grep_without_content_mode_has_no_match_count():
    html = _body("grep", {"pattern": "foo"}, result="Mode: files_with_matches\n\na.py\nb.py")
    assert '<span class="body-size">4 lines · 0.1 KB</span>' in html


def test_grep_shows_twelve_matches_and_the_rest_under_show_all():
    result = "a.py:\n" + "\n".join(f"  {i}: hit {i}" for i in range(1, 21))
    html = _body("grep", {"pattern": "hit", "output_mode": "content"}, result=result)
    visible, more = html.split('<details class="body-more">')
    assert visible.count("<mark>hit</mark>") == 12 and more.count("<mark>hit</mark>") == 8
    assert "Show all 20 lines" in more and "a.py" in more


def test_grep_counts_the_hits_past_what_the_view_keeps():
    result = "a.py:\n" + "\n".join(f"  {i}: hit {i}" for i in range(1, 601)) + "\nb.py:\n  1- context"
    html = _body("grep", {"pattern": "hit", "output_mode": "content"}, result=result)
    assert "Show all 499 lines" in html and "and 102 more" in html


def test_grep_keeps_the_note_that_the_search_stopped_early():
    result = "a.py:\n  1: foo\n\nNote: the search stopped early (it hit its time limit or the maximum match count)."
    assert "the search stopped early" in _body("grep", {"pattern": "foo", "output_mode": "content"}, result=result)


@pytest.mark.parametrize("name", ["glob", "ls"])
def test_paths_get_a_file_icon_and_no_folder_icon(name):
    seg = tool_call(
        "c1",
        name,
        {"pattern": "*", "path": "/workspace/repo"},
        result="['/workspace/repo/a/b.py', '/workspace/repo/daiv']",
    )
    html = _body_with(seg, "0", labels={"icons": ICONS})
    assert html.count('<li><svg data-icon="doc"></svg>') == 2 and 'data-icon="folder"' not in html
    assert "a/b.py" in html and "/workspace/repo/" not in html


@pytest.mark.parametrize("result", ["No files found", "No files found\n\nNote: some directories could not be read."])
def test_a_search_with_no_files_found_is_plain_text(result):
    html = _body("glob", {"pattern": "*.nope"}, result=result)
    assert "No files found" in html and "<li>" not in html


def test_a_cut_path_list_shows_its_notes_apart_from_the_paths():
    result = "['a.py', '... [results truncated, try being more specific with your parameters]']\n\nNote: stopped early."
    html = _body("glob", {"pattern": "*"}, result=result)
    assert html.count("<li>") == 1 and "results truncated" in html and "stopped early" in html


def test_a_newline_separated_listing_is_still_a_list():
    assert _body("ls", {"path": "/workspace/repo"}, result="a.py\nb.py").count("<li>") == 2


def test_bash_single_command_omits_the_command_line_and_lists_changes():
    result = json.dumps({
        "commands": [{"command": "make fmt", "exit_code": 0, "output": "ok"}],
        "files_changed": [{"path": "a.py", "op": "modified"}],
    })
    html = _body("bash", {"command": "make fmt"}, result=result)
    assert "make fmt" not in html and ">M<" in html and "a.py" in html


def test_bash_with_no_changes_has_no_changed_section():
    result = json.dumps({"commands": [{"command": "ls", "exit_code": 0, "output": "x"}], "files_changed": []})
    assert "Changed" not in _body("bash", {"command": "ls"}, result=result)


def test_bash_single_command_does_not_repeat_its_exit_code_and_sizes_its_output():
    result = json.dumps({"commands": [{"command": "pytest", "exit_code": 1, "output": "1 failed\n<done>"}]})
    html = _body("bash", {"command": "pytest"}, result=result)
    assert "1 failed\n&lt;done&gt;" in html and "exit 1" not in html and "pytest" not in html
    assert '<span class="body-size">2 lines · 0.1 KB</span>' in html


def test_bash_several_commands_show_each_command_and_its_failure():
    commands = [
        {"command": "make a", "exit_code": 0, "output": "A"},
        {"command": "make <b>", "exit_code": 2, "output": "B"},
    ]
    html = _body("bash", {"command": "make a; make b"}, result=json.dumps({"commands": commands, "files_changed": []}))
    assert html.count('<span class="body-prompt">$</span>') == 2 and "make a" in html and "make &lt;b&gt;" in html
    assert "exit 2" in html and "exit 0" not in html


def test_bash_changes_map_each_operation_and_show_renames():
    files = [
        {"path": "a.py", "op": "added"},
        {"path": "b.py", "op": "deleted"},
        {"path": "new.py", "op": "renamed", "from_path": "old.py"},
        {"path": "m.py", "op": "modified"},
    ]
    result = json.dumps({"commands": [{"command": "x", "exit_code": 0, "output": "done"}], "files_changed": files})
    changed = _body("bash", {"command": "x"}, result=result).split("Loaded into context")[0]
    assert all(f">{op}<" in changed for op in "ADRM") and "old.py → new.py" in changed


def test_bash_changes_count_only_the_entries_they_can_show():
    files = [{"path": "a.py", "op": "added"}, "junk", None]
    result = json.dumps({"commands": [{"command": "x", "exit_code": 0, "output": "done"}], "files_changed": files})
    changed = _body("bash", {"command": "x"}, result=result).split("Loaded into context")[0]
    assert "a.py" in changed and "more" not in changed


def test_bash_text_result_is_shown_as_text():
    html = _body("bash", {"command": "x"}, result="The sandbox is unavailable; retry once.")
    assert "The sandbox is unavailable; retry once." in html


def test_grep_never_runs_a_nested_quantifier_against_the_matches():
    result = "a.py:\n  1: def " + "a" * 28 + "()"
    started = time.monotonic()
    html = _body("grep", {"pattern": r"(\w+\s?)+:", "output_mode": "content"}, result=result)
    assert time.monotonic() - started < 5 and "<mark>" not in html and "a" * 28 in html


@pytest.mark.parametrize("pattern,text", [(r"a\Kb", "aKb"), ("[[:alpha:]]", "a]")], ids=["perl-keep", "posix-class"])
def test_grep_pattern_js_reads_differently_is_not_highlighted(pattern, text):
    assert "<mark>" not in _body("grep", {"pattern": pattern, "output_mode": "content"}, result=f"a.py:\n  1: {text}")


def test_grep_context_rows_are_never_marked():
    html = _body("grep", {"pattern": "foo", "output_mode": "content"}, result="a.py:\n  1- foo\n  2: foo")
    assert html.count("<mark>foo</mark>") == 1


def test_grep_marks_only_the_start_of_a_long_line():
    result = "a.py:\n  1: foo " + "x" * 400 + " foo"
    html = _body("grep", {"pattern": "foo", "output_mode": "content"}, result=result)
    assert html.count("<mark>foo</mark>") == 1 and "x" * 400 + " foo" in html


@pytest.mark.parametrize(
    "command",
    ["cat <<EOF > a.py\nprint(1)\nEOF", "uv run pytest " + " ".join(["tests/unit_tests/chat/x.py"] * 4)],
    ids=["multi-line", "long"],
)
def test_bash_command_the_row_cuts_is_shown_whole_as_input(command):
    result = json.dumps({"commands": [{"command": command, "exit_code": 0, "output": "ok"}]})
    html = _body("bash", {"command": command}, result=result)
    shown, context = html.split("Loaded into context")
    assert "Input" in shown and '<span class="body-prompt">$</span> ' in shown
    assert all(line.replace("<", "&lt;").replace(">", "&gt;") in shown for line in command.split("\n"))
    assert "ok" in context


def test_bash_short_command_has_no_input():
    result = json.dumps({"commands": [{"command": "make fmt", "exit_code": 0, "output": "ok"}]})
    html = _body("bash", {"command": "make fmt"}, result=result)
    assert "Input" not in html and "make fmt" not in html


def _seg(name: str, args: dict, *, result: str | None = None, status: str = "done", **extra) -> dict:
    return {**tool_call("c1", name, args, result=result, status=status), **extra}


def test_skill_body_links_to_skills_and_shows_no_text():
    url = "/skills/systematic-debugging/"
    seg = _seg("skill", {"skill": "systematic-debugging"}, result="# Long skill\n" * 100, skill_url=url)
    html = _body_seg(seg)
    assert 'href="/skills/systematic-debugging/"' in html and "Long skill" not in html


def test_skill_body_says_the_whole_skill_was_loaded_with_its_size():
    html = _body_seg(_seg("skill", {"skill": "debug"}, result="# Long skill\n" * 100, skill_url="/skills/debug/"))
    assert "The whole skill, always: skills are never cut to a file." in html
    assert '<span class="body-size">100 lines · 1.3 KB</span>' in html and "Open debug in Skills ↗" in html


@pytest.mark.parametrize("url", ["/skills/x/", "https://daiv.example/skills/x/"])
def test_skill_link_opens_in_a_new_tab(url):
    html = _body_seg(_seg("skill", {"skill": "x"}, result="body", skill_url=url))
    assert f'href="{url}" target="_blank" rel="noopener noreferrer">Open x in Skills ↗</a>' in html


def test_skill_without_a_skills_page_has_no_link():
    html = _body_seg(_seg("skill", {"skill": "repo-skill"}, result="body"))
    assert "<a " not in html and "in Skills" not in html


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "//evil.example/skills/x/", "/\t/evil.example/", '/skills/x/" onmouseover="alert(1)']
)
def test_skill_link_is_only_a_local_path_or_a_web_url(url):
    html = _body_seg(_seg("skill", {"skill": "x"}, result="body", skill_url=url))
    assert "<a " not in html


def test_task_prompt_is_markdown_clamped_to_three_lines():
    prompt = "**Goal**: fix it\nline two\nline three\nline four\n\nmore context\nand more"
    html = _body("task", {"description": prompt, "subagent_type": "explore"}, result="done")
    shown, context = html.split("Loaded into context")
    visible, more = shown.split('<details class="body-more">')
    assert "Input" in visible and "<strong>Goal</strong>" in visible and "more context" not in visible
    assert more.startswith("<summary>Show the whole prompt</summary>") and "more context" in more


def test_task_reply_is_markdown_and_the_subagent_inside_is_not_shown():
    html = _body("task", {"description": "x" * 200, "subagent_type": "explore"}, result="**found it**")
    context = html.split("Loaded into context")[1]
    assert "<strong>found it</strong>" in context
    assert html.index("found it") < html.index("What the subagent did inside is not shown.")


def test_task_prompt_the_row_shows_whole_is_not_repeated():
    html = _body("task", {"description": "Find the bug", "subagent_type": "explore"}, result="ok")
    assert "Input" not in html and "Find the bug" not in html


def test_web_search_lists_the_results_without_the_query():
    results = [{"title": "Fixtures", "link": "https://docs.pytest.org/f", "content": "How fixtures work"}]
    html = _body("web_search", {"query": "pytest fixtures guide"}, result=json.dumps(results))
    assert 'href="https://docs.pytest.org/f"' in html and "How fixtures work" in html
    assert "pytest fixtures guide" not in html


def test_web_search_without_results_leaves_it_to_the_row():
    html = _body("web_search", {"query": "q"}, result="[]")
    assert html == ""


def test_web_fetch_input_links_the_page_and_shows_what_was_asked():
    args = {"url": "https://docs.example.com/a?b=1", "prompt": "What is the **default** timeout?"}
    html = _body("web_fetch", args, result="30 seconds")
    shown = html.split("Loaded into context")[0]
    link = '<a class="chat-tool__link" href="https://docs.example.com/a?b=1" target="_blank" rel="noopener noreferrer">'
    assert "Input" in shown and "<dt>Page</dt>" in shown and link in shown
    assert "<dt>Asked</dt>" in shown and "<strong>default</strong>" in shown


def test_web_fetch_answer_is_called_a_summary_of_the_page():
    html = _body("web_fetch", {"url": "https://x.example/", "prompt": "p"}, result="**Yes**, it does.")
    assert '<span class="body-size">a summary of the page · 1 line · 0.1 KB</span>' in html
    assert "<strong>Yes</strong>" in html.split("Loaded into context")[1]


def test_web_fetch_page_contents_are_not_called_a_summary():
    result = "Contents of https://x.example/:\n# Hi"
    html = _body("web_fetch", {"url": "https://x.example/", "prompt": ""}, result=result)
    assert "summary" not in html and "Loaded into context" in html


def test_web_fetch_redirect_says_where_it_went():
    result = "<redirect_url>https://new.example/path</redirect_url>"
    context = _body("web_fetch", {"url": "https://old.example/path", "prompt": "p"}, result=result).split(
        "Loaded into context"
    )[1]
    assert "<dt>Redirected to</dt>" in context and 'href="https://new.example/path"' in context


def test_web_fetch_error_is_what_went_wrong():
    html = _body("web_fetch", {"url": "https://x.example/", "prompt": "p"}, result="error: Failed to fetch URL: boom")
    assert html.startswith('<div class="body-error"') and "Failed to fetch URL: boom" in html
    assert "Loaded into context" not in html


def test_web_fetch_page_that_is_not_a_web_url_is_not_a_link():
    html = _body("web_fetch", {"url": "javascript:alert(1)", "prompt": "p"}, result="x")
    assert 'href="javascript:' not in html and "javascript:alert(1)" in html


def test_gitlab_read_puts_title_first():
    html = _body(
        "gitlab",
        {"subcommand": "project-merge-request get --iid 1"},
        result="id: 9\nstate: opened\ntitle: Fix it\nweb_url: https://g/x/-/merge_requests/1",
    )
    assert html.index("Fix it") < html.index("opened")


def test_gitlab_read_links_the_first_field_to_its_web_url():
    result = "iid: 1\nid: 9\nsource-branch: fix\nstate: opened\ntitle: Fix it\nweb-url: https://g/x/-/merge_requests/1"
    html = _body("gitlab", {"subcommand": "project-merge-request get --iid 1"}, result=result)
    keys = re.findall(r"<dt>([^<]*)</dt>", html)
    assert keys[:3] == ["title", "state", "source-branch"] and "web-url" not in keys
    assert '<dd><a class="chat-tool__link" href="https://g/x/-/merge_requests/1"' in html


def test_gitlab_nested_object_reads_as_one_field_and_keeps_the_rest_under_show_all():
    result = "iid: 1\nauthor:\n  id: 5\n  state: active\n  username: bob\nstate: opened\ntitle: Fix it"
    html = _body("gitlab", {"subcommand": "project-merge-request get --iid 1"}, result=result)
    visible, rest = html.split('<details class="body-more">')
    assert "<dt>author</dt><dd>bob</dd>" in visible and "<dt>state</dt><dd>opened</dd>" in visible
    assert "active" not in visible and "<summary>Show all 7 lines</summary>" in rest and "  state: active" in rest


def test_gitlab_value_over_several_lines_stays_one_field():
    result = "iid: 1\ndescription: First para\n\nsecond para\nstate: opened\ntitle: T"
    html = _body("gitlab", {"subcommand": "project-merge-request get --iid 1"}, result=result)
    assert "<table" not in html and "<dd>First para\n\nsecond para</dd>" in html


def test_gitlab_list_of_several_objects_is_a_table():
    result = "iid: 1\ntitle: First\n\niid: 2\ntitle: Second\n\niid: 3\ntitle: Third\n"
    html = _body("gitlab", {"subcommand": "project-merge-request list --state opened"}, result=result)
    assert "<th>title</th><th>iid</th>" in html and html.count("<tr>") == 4
    assert "<td>Second</td><td>2</td>" in html


def test_gitlab_job_trace_marks_errors():
    html = _body("gitlab", {"subcommand": "project-job trace --id 3"}, result="ok\nFAILED test_x\ndone")
    assert 'class="lgl err"' in html and "Jump to first error" in html


def test_job_trace_opens_at_its_end_and_jumps_to_the_first_error():
    result = "setup\nerror: first\nERROR second\nTypeError: not marked\nend"
    html = _body("gitlab", {"subcommand": "project-job trace --id 3"}, result=result, id="call:1/x")
    assert '<div class="body-log"><div class="body-log-lines">' in html and html.count('class="lgl err"') == 2
    assert (
        html.count('id="log-err-call1x"') == 1
        and '<a class="body-jump chat-tool__link" href="#log-err-call1x">' in html
    )
    assert re.search(r'class="lgl err" id="log-err-call1x">error: first<', html)
    assert '<div class="lgl">TypeError: not marked</div>' in html


def test_job_trace_without_errors_has_no_jump():
    html = _body("gitlab", {"subcommand": "project-job trace --id 3"}, result="all good\ndone")
    assert "Jump to first error" not in html and 'class="lgl err"' not in html


def test_long_job_trace_keeps_its_end():
    result = "\n".join(f"step {i}" for i in range(1000))
    html = _body("gitlab", {"subcommand": "project-job trace --id 3"}, result=result)
    assert "step 999<" in html and "step 499<" not in html and "500 earlier lines not shown" in html


def test_gh_run_log_is_a_log_view():
    html = _body("gh", {"subcommand": "run view 5 --job 7 --log"}, result="build\tstep\tFAILED tests")
    assert 'class="lgl err"' in html


def test_gh_raw_view_shows_fields_then_the_body():
    result = "title:\tFix it\nstate:\tOPEN\nurl:\thttps://github.com/o/r/pull/3\n--\n**Body** text"
    html = _body("gh", {"subcommand": "pr view 3"}, result=result)
    assert '<dd><a class="chat-tool__link" href="https://github.com/o/r/pull/3"' in html
    assert "<dt>state</dt><dd>OPEN</dd>" in html and "<strong>Body</strong>" in html


GITLAB_MR = "https://g/x/-/merge_requests/1"
GITHUB_PR = "https://github.com/o/r/pull/12"


def test_gitlab_write_shows_what_was_posted():
    html = _body(
        "gitlab",
        {"subcommand": 'project-merge-request-note create --mr-iid 1 --body "**hi**"'},
        result="id: 552",
        mr_url="https://g/x/-/merge_requests/1",
    )
    assert "<strong>hi</strong>" in html and "#note_552" in html


def test_gitlab_write_changed_comes_before_the_reply_and_opens_the_note():
    subcommand = 'project-merge-request-note create --mr-iid=1 --body "it\'s **done**"'
    html = _body("gitlab", {"subcommand": subcommand}, result="id: 552", mr_url="https://g/x/-/merge_requests/1")
    changed, context = html.split("Loaded into context")
    assert "Changed" in changed and ">Open ↗<" in changed
    assert 'href="https://g/x/-/merge_requests/1#note_552"' in changed
    assert "<dt>id</dt><dd>552</dd>" in context


def test_gitlab_note_update_opens_the_note_not_its_author():
    result = "author:\n  id: 5\n  username: bob\nbody: edited\nid: 552\nsystem: False"
    subcommand = 'project-merge-request-note update --mr-iid 1 --id 552 --body "edited"'
    html = _body("gitlab", {"subcommand": subcommand}, result=result, mr_url=GITLAB_MR)
    assert 'href="https://g/x/-/merge_requests/1#note_552"' in html and '#note_5"' not in html


def test_gh_comment_links_to_the_comment_on_the_pull_request():
    html = _body(
        "gh",
        {"subcommand": 'pr comment 12 --body "**ok**"'},
        result="https://github.com/o/r/pull/12#issuecomment-345",
        mr_url="https://github.com/o/r/pull/12",
    )
    changed = html.split("Loaded into context")[0]
    assert "<strong>ok</strong>" in changed and 'href="https://github.com/o/r/pull/12#issuecomment-345"' in changed


@pytest.mark.parametrize(
    "name,subcommand,result",
    [
        ("gitlab", 'project-merge-request-note create --mr-iid 2 --body "x"', "id: 5"),
        ("gitlab", 'project-issue-note create --issue-iid 1 --body "x"', "id: 5"),
        ("gitlab", 'project-merge-request update --iid 1 --description "x"', "iid: 1"),
        ("gh", 'pr comment 13 --body "x"', "https://github.com/o/r/pull/13#issuecomment-9"),
        ("gh", 'issue comment 12 --body "x"', "https://github.com/o/r/issues/12#issuecomment-9"),
    ],
    ids=["other-mr", "issue-note", "mr-update", "other-pr", "issue-comment"],
)
def test_a_write_that_is_not_a_note_on_the_sessions_merge_request_has_no_open_link(name, subcommand, result):
    html = _body(name, {"subcommand": subcommand}, result=result, mr_url=GITLAB_MR if name == "gitlab" else GITHUB_PR)
    assert "Changed" in html and "Open ↗" not in html


def test_a_write_that_failed_shows_its_text_as_input():
    html = _body(
        "gitlab",
        {"subcommand": 'project-merge-request-note create --mr-iid 1 --body "**hi**"'},
        result="error: GitLab command failed (exit code 1). Details: 403",
        mr_url="https://g/x/-/merge_requests/1",
    )
    assert html.startswith('<div class="body-error"') and "Changed" not in html
    assert "Input" in html and "<strong>hi</strong>" in html and "#note_" not in html


def test_a_platform_write_without_a_body_has_no_changed():
    html = _body("gh", {"subcommand": "pr checks 12"}, result="build\tpass\t1m\thttps://x")
    assert "Changed" not in html and "build" in html


def test_platform_result_saved_to_a_file_on_request_says_so_with_its_size():
    result = "Wrote 20480 bytes (400 lines) to /workspace/tmp/large_tool_results/c1\nPreview (first 1 lines):\n[{"
    args = {"subcommand": "project-merge-request list --get-all", "output_to_file": True}
    html = _body("gitlab", args, result=result)
    assert "Saved to /workspace/tmp/large_tool_results/c1 as asked: the model saw only a preview." in html
    assert "400 lines · 20 KB" in html and "Too large" not in html


def test_an_evicted_result_is_still_too_large_to_load_whole():
    html = _body("gitlab", {"subcommand": "project-job trace --id 3"}, result=EVICTED)
    assert "Too large to load whole" in html and "as asked" not in html


def test_gitlab_crlf_value_keeps_its_field():
    result = "iid: 1\ncreated-at: 2024-01-01\ndescription: First line\r\nsecond line\r\nstate: opened\r\ntitle: T"
    html = _body("gitlab", {"subcommand": "project-merge-request get --iid 1"}, result=result)
    assert "<dt>created-at</dt><dd>2024-01-01</dd>" in html and "<dt>state</dt><dd>opened</dd>" in html
    assert "<dt>description</dt><dd>First line\nsecond line</dd>" in html and "body-more" not in html


def test_gitlab_crlf_note_body_after_a_nested_author_is_kept():
    author = "author:\n  id: 5\n  username: bob"
    note = f"id: {{}}\nattachment: None\n{author}\nbody: Please fix\r\n\r\nthe tests\r\nsystem: False"
    result = f"{note.format(1)}\n\n{note.format(2)}"
    html = _body("gitlab", {"subcommand": "project-merge-request-note list --mr-iid 1"}, result=result)
    visible = html.split('<details class="body-more">')[0]
    assert "<th>body</th>" in visible and visible.count("<td>Please fix\n\nthe tests</td>") == 2


def test_gh_crlf_view_keeps_its_fields_and_body():
    result = "title:\tFix it\r\nstate:\tOPEN\r\n--\r\n**Body** text\r\n"
    html = _body("gh", {"subcommand": "pr view 3"}, result=result)
    assert "<dt>title</dt><dd>Fix it</dd><dt>state</dt><dd>OPEN</dd>" in html and "<strong>Body</strong>" in html


@pytest.mark.parametrize(
    "result,line",
    [
        ("iid: 1\nauthor:\n  username: bob\nstray line\ntitle: T", "stray line"),
        ("iid: 1\nauthor:\n  username: bob\n\nstray line\ntitle: T", "stray line"),
        ("iid: 1\nauthor:\n    username: bob\n  odd: x\ntitle: T", "  odd: x"),
    ],
    ids=["unindented", "after-a-blank", "shallower"],
)
def test_gitlab_line_the_field_list_cannot_place_falls_back_to_text(result, line):
    html = _body("gitlab", {"subcommand": "project-merge-request get --iid 1"}, result=result)
    assert "<dl" not in html and line in html


def test_gitlab_table_columns_it_cannot_fit_stay_under_show_all():
    record = "\n".join(f"k{i}: v{i}" for i in range(8))
    html = _body("gitlab", {"subcommand": "project-issue list"}, result=f"{record}\n\n{record}")
    visible, rest = html.split('<details class="body-more">')
    assert visible.count("<th>") == 6 and "v7" not in visible and "k7: v7" in rest


def test_gitlab_table_row_missing_its_first_column_still_shows_its_link():
    result = "iid: 1\ntitle: A\nweb-url: https://g/1\n\niid: 2\nweb-url: https://g/2"
    html = _body("gitlab", {"subcommand": "project-merge-request list"}, result=result)
    assert 'href="https://g/2" target="_blank" rel="noopener noreferrer">https://g/2</a>' in html


def test_gitlab_repeated_key_keeps_the_earlier_value_under_show_all():
    result = "iid: 1\ndescription: a\ntitle: inner\nmore\ntitle: Real"
    html = _body("gitlab", {"subcommand": "project-merge-request get --iid 1"}, result=result)
    visible, rest = html.split('<details class="body-more">')
    assert "<dd>Real</dd>" in visible and "inner" not in visible and "title: inner\nmore" in rest


def test_gitlab_value_cut_in_the_field_list_is_whole_under_show_all():
    result = f"iid: 1\ndescription: {'d' * 1500}\ntitle: T"
    visible, rest = _body("gitlab", {"subcommand": "project-issue get --iid 1"}, result=result).split(
        '<details class="body-more">'
    )
    assert "d" * 1000 + "…" in visible and "d" * 1001 not in visible and "d" * 1500 in rest


SERVERS = [{"name": "sentry", "read_only_tools": ["search_issues"], "icon": ""}]


def test_mcp_read_only_tool_shows_context_and_tag():
    html = _body("sentry_search_issues", {"query": "x"}, result=json.dumps([{"id": 1}]), servers=SERVERS)
    assert "read-only tool" in html and "Loaded into context" in html and "Changed" not in html


def test_mcp_tool_that_can_change_things_leads_with_changed():
    html = _body("sentry_create_issue", {"title": "x"}, result="Created https://s/x", servers=SERVERS)
    assert "can change things" in html and "Changed" in html and 'href="https://s/x"' in html


def test_mcp_tool_of_a_removed_server_uses_the_fallback():
    html = _body("gone_do_thing", {"a": 1}, result="ok", servers=SERVERS)
    assert "read-only tool" not in html and "can change things" not in html


def test_mcp_input_lists_the_other_arguments_with_non_strings_as_json():
    args = {"query": "is:unresolved", "limit": 5, "filters": {"a": [1, 2]}, "flag": True}
    html = _body("sentry_search_issues", args, result="[]", servers=SERVERS)
    assert '<div class="body-label">Input <span class="body-tag body-tag--read-only">read-only tool</span>' in html
    assert '<dt>limit</dt><dd>5</dd><dt>filters</dt><dd>{"a":[1,2]}</dd><dt>flag</dt><dd>true</dd>' in html
    assert "is:unresolved" not in html


@pytest.mark.parametrize("query", ["x" * 81, "two\nlines", "two  spaces"])
def test_mcp_argument_the_row_cut_is_in_the_input(query):
    html = _body("sentry_search_issues", {"query": query, "limit": 5}, result="[]", servers=SERVERS)
    assert "<dt>query</dt>" in html


def test_mcp_tag_goes_on_the_result_when_the_row_shows_every_argument():
    html = _body("sentry_search_issues", {"query": "x"}, result="[]", servers=SERVERS)
    assert "Input" not in html
    assert '<div class="body-label">Loaded into context <span class="body-tag body-tag--read-only">' in html


def test_mcp_change_comes_first_and_holds_the_result_with_no_context_section():
    result = json.dumps({"id": 7, "url": "https://s/issues/7"})
    html = _body("sentry_create_issue", {"title": "x", "project": "p"}, result=result, servers=SERVERS)
    assert html.index("Changed") < html.index("Input") and "Loaded into context" not in html
    assert "<dt>id</dt><dd>7</dd>" in html and 'body-tag--can-change">can change things' in html


def test_mcp_change_links_are_http_only_and_cannot_break_out():
    result = 'Created https://s/x. Also javascript:alert(1) and https://s/y"onmouseover=alert(1)'
    html = _body("sentry_create_issue", {}, result=result, servers=SERVERS)
    buttons = html.split('<p class="body-buttons">')[1]
    assert 'href="https://s/x"' in buttons and 'href="https://s/y"' in buttons
    assert "javascript" not in buttons and "onmouseover" not in buttons


def test_mcp_change_that_failed_shows_no_changed():
    html = _body("sentry_create_issue", {"title": "x"}, result="Error: forbidden", servers=SERVERS)
    assert html.startswith('<div class="body-error"') and "Changed" not in html


def test_mcp_offloaded_change_still_says_so():
    html = _body("sentry_create_issue", {"title": "x"}, result=EVICTED, servers=SERVERS)
    assert "Changed" in html and "Too large to load whole" in html


def test_mcp_server_never_synced_gets_no_tag():
    rows = [{"name": "sentry", "read_only_tools": [], "icon": "", "synced": False}]
    html = _body("sentry_create_issue", {"title": "x"}, result="ok", servers=rows)
    assert "can change things" not in html and "read-only tool" not in html and "Loaded into context" in html


def test_mcp_long_argument_is_cut_in_the_field_list_and_whole_under_show_all():
    html = _body("sentry_create_issue", {"body": "d" * 1500}, result="ok", servers=SERVERS)
    visible, rest = html.split('<details class="body-more">', 1)
    assert "d" * 1000 + "…" in visible and "d" * 1001 not in visible and "d" * 1500 in rest


def _mcp_row(name: str, rows: list[dict]) -> dict:
    seg = tool_call("c1", name, {"query": "x"}, result="ok")
    ctx = {"mcpRows": rows}
    return run_transcript("toolRow(vars.seg, vars.ctx)", {"seg": seg, "ctx": ctx}, blocks=False)


@pytest.mark.parametrize(
    "icon,src",
    [
        ("https://x.test/a.svg", "https://x.test/a.svg"),
        ("data:image/png;base64,AAAA", "data:image/png;base64,AAAA"),
        ("", ""),
        ("javascript:alert(1)", ""),
        ("http://x.test/a.png", ""),
        ("data:text/html,<script>alert(1)</script>", ""),
    ],
)
def test_mcp_row_uses_the_servers_own_icon_only_when_safe(icon, src):
    row = _mcp_row("sentry_search_issues", [{"name": "sentry", "read_only_tools": [], "icon": icon}])
    assert (row["iconSrc"], row["icon"]) == (src, "puzzle-piece")


def test_rows_of_other_tools_have_no_icon_source():
    assert _row("read_file", {"file_path": "/workspace/repo/a.py"})["iconSrc"] == ""


TOOL_SEARCH_HEADER = "Loaded 2 tool(s). Their full schemas follow — call them directly by name."


def _schema(name: str, description: str, *params: str) -> str:
    properties = {param: {"type": "string"} for param in params}
    schema = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties},
        },
    }
    return f"<function>{json.dumps(schema, separators=(',', ':'))}</function>"


def test_tool_search_lists_tools_not_schemas():
    result = (
        "Loaded 1 tool(s). Their full schemas follow — call them directly by name.\n\n<functions>\n"
        '<function>{"type":"function","function":{"name":"gitlab","description":"Run a CLI. More.",'
        '"parameters":{"properties":{"subcommand":{}}}}}</function>\n</functions>'
    )
    html = _body("tool_search", {"select": ["gitlab"]}, result=result)
    assert "gitlab" in html and "subcommand" in html and '"type":"function"' not in html


def test_tool_search_entry_has_the_first_sentence_and_parameter_chips():
    body = "\n".join([
        _schema("gitlab", "Run a CLI. More text.\nSecond line.", "subcommand", "output_mode"),
        _schema("gh", "GitHub CLI.", "subcommand"),
    ])
    result = f"{TOOL_SEARCH_HEADER}\n\n<functions>\n{body}\n</functions>"
    html = _body("tool_search", {"select": ["gitlab", "gh"]}, result=result)
    assert "Run a CLI." in html and "More text" not in html and "Second line" not in html
    assert '<span class="body-chip">subcommand</span><span class="body-chip">output_mode</span>' in html
    size = len(result.encode()) / 1024
    assert f'<span class="body-size">2 tool schemas · {size:.1f} KB</span>' in html
    assert "Loaded 2 tool(s)" not in html


def test_tool_search_summaries_and_ignored_names_are_listed():
    body = "\n".join([
        _schema("gitlab", "Run a CLI.", "subcommand"),
        "<function-summary>repo_tool: Works on a repo object</function-summary>",
    ])
    result = f"{TOOL_SEARCH_HEADER}\n\n<functions>\n{body}\n</functions>\n\nIgnored unknown names: nope"
    html = _body("tool_search", {"select": ["gitlab", "repo_tool", "nope"]}, result=result)
    assert "repo_tool" in html and "Works on a repo object" in html
    assert "1 tool schema · " in html and "Ignored unknown names: nope" in html


def test_tool_search_summary_lines_form_lists_each_tool_with_the_default_size():
    result = "Loaded 2 tool(s):\n- gitlab: Run a CLI\n- gh: GitHub CLI"
    html = _body("tool_search", {"query": "cli"}, result=result)
    assert "Run a CLI" in html and "GitHub CLI" in html and "- gitlab:" not in html
    assert "tool schema" not in html and "3 lines · " in html


@pytest.mark.parametrize(
    "result,shown",
    [
        ("No deferred tools matched query 'x'.", "No deferred tools matched query"),
        (f"{TOOL_SEARCH_HEADER}\n\n<functions>\n<function>{{nope</function>\n</functions>", "{nope"),
    ],
)
def test_tool_search_text_it_cannot_list_is_shown_as_is(result, shown):
    assert shown in _body("tool_search", {"query": "x"}, result=result)


PUBLISHED = {
    "status": "published",
    "id": "a1",
    "title": "Report",
    "filename": "report.md",
    "content_type": "text/markdown",
    "kind": "markdown",
    "size": 2048,
    "url": "https://d/dashboard/artifacts/a1/",
    "download_url": "https://d/dashboard/artifacts/a1/download/",
}


def _body_artifacts(seg: dict, latest: dict) -> str:
    expr = "toolBodyHTML(vars.seg, { latestArtifactsById: new Map(Object.entries(vars.latest)) })"
    values = {"seg": seg, "latest": latest}
    return run_transcript(expr, values, blocks=False, labels={"show_raw": False}, markdown=True)


def test_publish_artifact_body_shows_the_artifact_then_status_and_url():
    html = _body("publish_artifact", {"path": "/workspace/tmp/report.md"}, result=json.dumps(PUBLISHED))
    changed, context = html.split("Loaded into context")
    assert "Changed" in changed and 'href="https://d/dashboard/artifacts/a1/"' in changed and ">Report</a>" in changed
    assert (
        '<span class="body-meta">Markdown</span><span class="body-meta">2.0 KB</span><span class="body-meta">new</span>'
        in changed
    )
    assert 'href="https://d/dashboard/artifacts/a1/download/"' in changed and "Download" in changed
    assert "published · " in context and 'href="https://d/dashboard/artifacts/a1/"' in context


def test_publish_artifact_body_links_the_latest_version_but_names_this_calls_own_change():
    newer = {**PUBLISHED, "status": "updated", "title": "Report v2", "url": "https://d/dashboard/artifacts/a1/?v=2"}
    seg = tool_call("c1", "publish_artifact", {"path": "/workspace/tmp/report.md"}, result=json.dumps(PUBLISHED))
    changed, context = _body_artifacts(seg, {"a1": newer}).split("Loaded into context")
    assert ">Report v2</a>" in changed and 'href="https://d/dashboard/artifacts/a1/?v=2"' in changed
    assert '<span class="body-meta">new</span>' in changed and "updated" not in changed
    assert "published · " in context and "?v=2" not in context


def test_publish_artifact_that_updated_says_so_whatever_the_latest_version():
    updated = {**PUBLISHED, "status": "updated"}
    seg = tool_call("c1", "publish_artifact", {"path": "/workspace/tmp/report.md"}, result=json.dumps(updated))
    changed = _body_artifacts(seg, {"a1": {**PUBLISHED, "status": "published"}}).split("Loaded into context")[0]
    assert '<span class="body-meta">updated</span>' in changed and ">new<" not in changed


def test_publish_artifact_relative_url_and_warning_are_shown():
    result = json.dumps({"status": "published", "id": "a1", "url": "/dashboard/artifacts/a1/", "warning": "Stored."})
    html = _body("publish_artifact", {"path": "/workspace/tmp/r.md"}, result=result)
    assert 'href="/dashboard/artifacts/a1/"' in html and "Stored." in html


def test_publish_artifact_error_is_what_went_wrong_only():
    html = _body("publish_artifact", {"path": "/x"}, result="Error publishing artifact '/x': too big")
    assert html.startswith('<div class="body-error"') and "Changed" not in html and "too big" in html


FETCHED = (
    "Fetched artifact 'a1' (report.md, 2048 bytes) to /workspace/tmp/report.md. Edit it, then call "
    '`publish_artifact(path="/workspace/tmp/report.md", artifact_id="a1")` to update the artifact in place.'
)


def test_fetch_artifact_shows_the_file_it_wrote():
    html = _body("fetch_artifact", {"artifact_id": "a1"}, result=FETCHED)
    changed = html.split("Loaded into context")[0]
    assert '<span class="body-op">A</span>' in changed and "/workspace/tmp/report.md" in changed
    assert "2.0 KB" in changed and "<a " not in changed


def test_fetch_artifact_links_an_artifact_the_page_knows():
    seg = tool_call("c1", "fetch_artifact", {"artifact_id": "a1"}, result=FETCHED)
    changed = _body_artifacts(seg, {"a1": PUBLISHED}).split("Loaded into context")[0]
    assert 'href="https://d/dashboard/artifacts/a1/"' in changed and ">Report</a>" in changed


DELEGATE_ARGS = {
    "goal": "Fix lint",
    "targets": [
        {"repo_id": "a/x", "ref": "main", "prompt": "Fix lint in x\nand more"},
        {"repo_id": "a/y", "prompt": "Fix lint in y"},
        {"repo_id": "a/z", "prompt": "Fix z"},
    ],
}


def _leg(repo_id: str, ref: str, thread_id: str, **extra) -> dict:
    return {"repo_id": repo_id, "ref": ref, "thread_id": thread_id, "session_url": f"https://d/s/{thread_id}/", **extra}


def test_delegate_jobs_whole_call_error_is_what_went_wrong():
    html = _body("delegate_jobs", {"goal": "g", "targets": []}, result=json.dumps({"error": "nope"}))
    assert html.startswith('<div class="body-error"') and "nope" in html


def test_delegate_jobs_table_joins_targets_with_started_and_failed_legs():
    result = {
        "batch_id": "b-1",
        "started": [
            _leg("a/x", "main", "t-x", starts_after_this_turn=False),
            _leg("a/y", "", "t-y", starts_after_this_turn=True),
        ],
        "failed": [{"repo_id": "a/z", "ref": "", "error": "Repository access denied.", "still_runs": False}],
    }
    html = _body("delegate_jobs", DELEGATE_ARGS, result=json.dumps(result))
    assert not html.startswith('<div class="body-error"')
    changed, rest = html.split('<div class="body-label">Input', 1)
    assert "<th>Repository</th><th>Branch</th><th>Job</th><th>Session</th>" in changed
    assert "<td>a/x</td><td>main</td><td>Fix lint in x</td>" in changed and "and more" not in changed
    assert 'href="/dashboard/sessions/t-x/"' in changed and "Session →" in changed
    assert "<td>a/y</td><td>default branch</td><td>Fix lint in y</td>" in changed
    assert "starts after this turn" in changed
    assert '<tr class="body-row-failed"><td>a/z</td><td>default branch</td><td>Fix z</td>' in changed
    assert "Repository access denied." in changed
    assert "Fix lint" in rest and "batch b-1 · 2 started" in rest.split("Loaded into context")[1]


def test_delegate_jobs_still_running_shows_the_goal_and_targets_as_input():
    html = _body("delegate_jobs", DELEGATE_ARGS, result=None, status="running")
    assert "Changed" not in html and "<dt>Goal</dt><dd>Fix lint</dd>" in html
    assert "<th>Repository</th><th>Branch</th><th>Job</th></tr>" in html and "<td>Fix z</td>" in html


def test_delegate_jobs_legs_of_the_same_repository_and_branch_match_in_order():
    args = {"goal": "g", "targets": [{"repo_id": "a/x", "prompt": "one"}, {"repo_id": "a/x", "prompt": "two"}]}
    result = {"batch_id": "b", "started": [_leg("a/x", "", "t1"), _leg("a/x", "", "t2")], "failed": []}
    html = _body("delegate_jobs", args, result=json.dumps(result))
    assert html.index("one") < html.index("/t1/") < html.index("two") < html.index("/t2/")


def test_delegate_jobs_where_no_leg_started_is_what_went_wrong():
    result = {
        "batch_id": None,
        "started": [],
        "failed": [{"repo_id": "a/x", "ref": "", "error": "Repository access denied.", "still_runs": False}],
        "note": "No sub-job is running and no resume will come.",
    }
    html = _body(
        "delegate_jobs", {"goal": "g", "targets": [{"repo_id": "a/x", "prompt": "p"}]}, result=json.dumps(result)
    )
    assert html.startswith('<div class="body-error"') and "Repository access denied." in html
    assert "Changed" not in html and "No sub-job is running" in html


@pytest.mark.parametrize(
    "session_url,href", [("https://d/s/x/", 'href="https://d/s/x/"'), ("javascript:alert(1)", None)]
)
def test_delegate_jobs_session_link_falls_back_to_a_safe_session_url(session_url, href):
    leg = {"repo_id": "a/x", "ref": "", "thread_id": "t/../x", "session_url": session_url}
    args = {"goal": "g", "targets": [{"repo_id": "a/x", "prompt": "p"}]}
    html = _body("delegate_jobs", args, result=json.dumps({"batch_id": "b", "started": [leg], "failed": []}))
    assert "/dashboard/sessions/" not in html and 'href="javascript' not in html
    assert href in html if href else "Session →" not in html


@pytest.mark.parametrize(
    ("url", "label"),
    [
        ("https://g/a/y/-/merge_requests/7", "!7"),
        ("https://github.com/a/y/pull/12", "#12"),
        ("https://g/a/y/-/tree/main", "merge request"),
        ("javascript:alert(1)", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_a_merge_request_label_is_its_number_for_a_web_link_only(url, label):
    assert run_transcript("mergeRequestLabel(vars.url)", {"url": url}, blocks=False) == label


def test_delegate_jobs_session_link_uses_session_url_without_the_page_path():
    leg = {"repo_id": "a/x", "ref": "", "thread_id": "t-x", "session_url": "https://d/s/t-x/"}
    args = {"goal": "g", "targets": [{"repo_id": "a/x", "prompt": "p"}]}
    seg = tool_call("c1", "delegate_jobs", args, result=json.dumps({"batch_id": "b", "started": [leg], "failed": []}))
    html = run_transcript("toolBodyHTML(vars.seg, {})", {"seg": seg}, blocks=False)
    assert 'href="https://d/s/t-x/"' in html and "/dashboard/sessions/" not in html


@pytest.mark.parametrize(
    "name,result",
    [
        ("delegate_jobs", "[not json"),
        ("delegate_jobs", json.dumps({"batch_id": "b", "started": "x", "failed": 5})),
        ("delegate_jobs", json.dumps({"batch_id": "b", "started": [None, 3, {"repo_id": 1}], "failed": [None]})),
        ("check_delegated_jobs", json.dumps({"batches": [None, 3, {"legs": "x"}, {"legs": [None, 1]}]})),
        ("cancel_delegated_jobs", json.dumps({"cancelled": [1, None], "legs": [None], "note": 5})),
        ("cancel_delegated_jobs", json.dumps([1, 2])),
    ],
)
def test_jobs_bodies_never_throw_on_odd_results(name, result):
    assert isinstance(_body(name, {"goal": 1, "targets": "x", "batch_id": 2}, result=result), str)


CHECKED = {
    "batches": [
        {
            "batch_id": "b-1",
            "finished": False,
            "legs": [
                _leg("a/x", "main", "t-x", status="running", branch="", merge_request_url="", reply=""),
                _leg(
                    "a/y",
                    "",
                    "t-y",
                    status="succeeded",
                    branch="daiv/fix",
                    merge_request_url="https://g/a/y/-/merge_requests/7",
                    reply="Fixed the lint.\nDetails here.",
                ),
            ],
        }
    ]
}


def test_check_delegated_jobs_shows_a_table_per_batch():
    html = _body("check_delegated_jobs", {"batch_id": "b-1"}, result=json.dumps(CHECKED))
    context = html.split("Loaded into context")[1]
    assert "<th>Repository</th><th>Branch</th><th>Status</th><th>Result</th><th>Session</th>" in context
    assert "<td>a/x</td><td>main</td><td>Running</td><td></td>" in context
    assert 'href="https://g/a/y/-/merge_requests/7"' in context and ">!7</a>" in context
    assert "Fixed the lint." in context and "Show the whole reply" in context and "Details here." in context
    assert 'href="/dashboard/sessions/t-y/"' in context and "batch b-1" not in context


def test_check_delegated_jobs_names_each_batch_when_it_reports_several():
    second = {**CHECKED["batches"][0], "batch_id": "b-2"}
    result = json.dumps({"batches": [CHECKED["batches"][0], second]})
    html = _body("check_delegated_jobs", {}, result=result)
    assert html.index("batch b-1") < html.index("batch b-2") and html.count("<table") == 2


@pytest.mark.parametrize(
    "word,label",
    [
        ("queued", "Queued"),
        ("running", "Running"),
        ("succeeded", "Finished"),
        ("failed", "Failed"),
        ("stopped to ask a question", "Waiting for an answer"),
        ("stopping", "Stopping"),
        ("cancelled", "Cancelled"),
        ("started, but DAIV lost track of it", "Lost"),
    ],
)
def test_check_and_cancel_bodies_show_the_status_vocabulary_of_the_card(word, label):
    leg = _leg("a/x", "main", "t-x", status=word)
    checked = _body("check_delegated_jobs", {}, result=json.dumps({"batches": [{"batch_id": "b", "legs": [leg]}]}))
    assert f"<td>{label}</td>" in checked and f">{word}<" not in checked.split("Loaded into context")[1]
    cancelled = _body("cancel_delegated_jobs", {}, result=json.dumps({"cancelled": ["t-x"], "legs": [leg]}))
    assert f"a/x \u00b7 main \u00b7 {label}" in cancelled


def test_check_delegated_jobs_shows_an_unknown_status_as_it_is_escaped_and_the_note():
    batch = {"batch_id": "b", "finished": True, "legs": [_leg("a/x", "", "t", status="<b>odd</b>")]}
    html = _body("check_delegated_jobs", {"batch_id": "b"}, result=json.dumps({"batches": [batch]}))
    assert "&lt;b&gt;odd&lt;/b&gt;" in html
    none = _body("check_delegated_jobs", {}, result=json.dumps({"batches": [], "note": "No sub-jobs."}))
    assert "No sub-jobs." in none


def test_check_delegated_jobs_error_is_what_went_wrong():
    html = _body("check_delegated_jobs", {"batch_id": "b"}, result=json.dumps({"error": "no batch b"}))
    assert html.startswith('<div class="body-error"') and "no batch b" in html


CANCELLED = {
    "batch_id": "b-1",
    "finished": False,
    "legs": [_leg("a/x", "main", "t-x", status="stopping"), _leg("a/y", "", "t-y", status="succeeded")],
    "cancelled": ["t-x"],
    "note": "You are resumed once the legs still queued, running or stopping have finished.",
}


def test_cancel_delegated_jobs_lists_each_cancelled_leg_then_the_note():
    html = _body("cancel_delegated_jobs", {"batch_id": "b-1"}, result=json.dumps(CANCELLED))
    changed, context = html.split("Loaded into context")
    assert "Changed" in changed and "a/x" in changed and "Stopping" in changed and "a/y" not in changed
    assert 'href="/dashboard/sessions/t-x/"' in changed
    assert "You are resumed once" in context and "a/y" not in context


def test_cancel_delegated_jobs_that_cancelled_nothing_has_no_changed():
    result = {**CANCELLED, "cancelled": [], "note": "Nothing was cancelled."}
    html = _body("cancel_delegated_jobs", {"batch_id": "b-1"}, result=json.dumps(result))
    assert "Changed" not in html and "Nothing was cancelled." in html


def test_cancel_delegated_jobs_error_is_what_went_wrong():
    html = _body("cancel_delegated_jobs", {"batch_id": "b"}, result=json.dumps({"error": "not a batch"}))
    assert html.startswith('<div class="body-error"') and "not a batch" in html


def test_json_table_columns_it_cannot_fit_stay_under_show_all():
    result = json.dumps([{f"k{i}": f"v{i}" for i in range(10)}, {"k0": "w0", "extra": "e"}])
    visible, rest = _body("some_tool", {}, result=result).split('<details class="body-more">')
    assert visible.count("<th>") == 6 and "v9" not in visible and "extra" not in visible
    assert '"k9": "v9"' in rest and '"extra": "e"' in rest


def test_json_rows_past_fifty_stay_under_show_all():
    visible, rest = _body("some_tool", {}, result=json.dumps([{"id": i} for i in range(60)])).split(
        '<details class="body-more">'
    )
    assert visible.count("<tr>") == 51 and "and 10 more" in visible and '"id": 59' in rest


def test_json_value_cut_in_the_field_list_is_whole_under_show_all():
    result = json.dumps({"description": "d" * 1500, "state": "opened"})
    visible, rest = _body("some_tool", {}, result=result).split('<details class="body-more">')
    assert "d" * 1000 + "…" in visible and "d" * 1001 not in visible and "d" * 1500 in rest


@pytest.mark.parametrize("data", [[{"id": 1, "title": "x"}, {"id": 2}], {"state": "opened"}], ids=["table", "fields"])
def test_json_view_that_leaves_nothing_out_has_no_show_all(data):
    assert "Show all" not in _body("some_tool", {}, result=json.dumps(data))


def test_json_show_all_stays_bounded():
    data = [{"id": i, "k": "v" * 50} for i in range(5000)]
    rest = _body("some_tool", {}, result=json.dumps(data)).split('<details class="body-more">')[1]
    total = len(json.dumps(data, indent=2).split("\n"))
    assert "Show all 500 lines" in rest and f"and {total - 500} more" in rest


WRITE_FAILED = (
    "Failed to write file '/workspace/repo/a.py': already exists — modify it with edit_file "
    "(write_file only creates new files)"
)


def test_a_failed_file_write_reads_failed():
    assert _row("write_file", {"file_path": "/workspace/repo/a.py", "content": "a\n"}, result=WRITE_FAILED)["failed"]
    done = _row("write_file", {"file_path": "/workspace/repo/a.py", "content": "a\n"}, result="Updated file a.py")
    assert done["failed"] is False


def test_a_failed_file_write_leads_with_what_went_wrong_and_changes_nothing():
    html = _body("write_file", {"file_path": "/workspace/repo/a.py", "content": "a\nb\n"}, result=WRITE_FAILED)
    assert html.startswith('<div class="body-error"') and html.count("already exists") == 1
    assert "Changed" not in html and "diff-add" not in html and "Loaded into context" not in html


def test_a_failed_edit_reads_failed_and_changes_nothing():
    args = {"file_path": "/workspace/repo/a.py", "old_string": "a\n", "new_string": "b\n"}
    result = "Error editing file '/workspace/repo/a.py': no match for old_string"
    assert _row("edit_file", args, result=result)["failed"] is True
    html = _body("edit_file", args, result=result)
    assert html.startswith('<div class="body-error"') and "Changed" not in html


@pytest.mark.parametrize("name", ["read_file", "bash", "some_tool"])
def test_write_failure_wording_fails_only_a_file_write(name):
    args = {"file_path": "/workspace/repo/a.py"}
    assert _row(name, args, result=WRITE_FAILED)["failed"] is False
    assert not _body(name, args, result=WRITE_FAILED).startswith('<div class="body-error"')


@pytest.mark.parametrize("name", ["check_delegated_jobs", "cancel_delegated_jobs"])
def test_a_delegated_job_check_that_errored_reads_failed(name):
    assert _row(name, {"batch_id": "b"}, result=json.dumps({"error": "no batch b"}))["failed"] is True
    assert _row(name, {"batch_id": "b"}, result=json.dumps({"batch_id": "b", "legs": []}))["failed"] is False


@pytest.mark.parametrize(
    "pattern,text",
    [
        (r"x\s*\s*\s*\s*y", "x" + " " * 290 + "xy"),
        (r"\s*\s*\s*\s*\s*y", " " * 290 + "xy"),
        (r"(a|aa)*c", "a" * 40 + "bc"),
    ],
    ids=["three-unbounded", "five-unbounded", "repeated-alternation"],
)
def test_grep_never_runs_a_pattern_that_backtracks_badly_against_the_matches(pattern, text):
    started = time.monotonic()
    html = _body("grep", {"pattern": pattern, "output_mode": "content"}, result=f"a.py:\n  1: {text}")
    assert time.monotonic() - started < 5 and "<mark>" not in html and text in html


@pytest.mark.parametrize(
    "pattern,text,marked",
    [(r"def\s+\w+", "def run(x):", "def run"), ("foo|bar", "a foo b", "foo"), (r"a.*b", "xaab", "aab")],
)
def test_grep_still_marks_ordinary_patterns(pattern, text, marked):
    html = _body("grep", {"pattern": pattern, "output_mode": "content"}, result=f"a.py:\n  1: {text}")
    assert f"<mark>{marked}</mark>" in html


def test_web_search_answer_without_a_title_is_labelled_in_the_page_language():
    result = json.dumps([{"title": "", "link": "", "content": "Use fixtures."}])
    assert "Suggested answer" in _body("web_search", {"query": "q"}, result=result)
    seg = tool_call("c1", "web_search", {"query": "q"}, result=result)
    labels = {"labels": {"suggested_answer": "Resposta sugerida"}}
    html = run_transcript("toolBodyHTML(vars.seg, {})", {"seg": seg}, blocks=False, labels=labels)
    assert "Resposta sugerida" in html and "Suggested answer" not in html


def test_a_body_builder_that_throws_falls_back_to_the_result_as_text():
    args = {"file_path": "/workspace/repo/a.py", "old_string": "a\n", "new_string": "b\n"}
    seg = tool_call("c1", "edit_file", args, result="Successfully replaced 1 instance(s) of the string in 'a.py'")
    html = _body_with(seg, 'window.Diff = { diffLines() { throw new Error("boom"); } }')
    assert html.startswith('<div class="body-section"><div class="body-label">Loaded into context')
    assert '<pre class="body-pre">Successfully replaced 1 instance(s)' in html and "Changed" not in html
