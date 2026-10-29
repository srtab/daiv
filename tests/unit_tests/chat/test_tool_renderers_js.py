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


def test_gitlab_nested_object_reads_as_one_field():
    result = "iid: 1\nauthor:\n  id: 5\n  state: active\n  username: bob\nstate: opened\ntitle: Fix it"
    html = _body("gitlab", {"subcommand": "project-merge-request get --iid 1"}, result=result)
    assert "<dt>author</dt><dd>bob</dd>" in html and "<dt>state</dt><dd>opened</dd>" in html and "active" not in html


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
    assert html.count('id="log-err-call1x"') == 1 and '<a class="body-jump" href="#log-err-call1x">' in html
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
