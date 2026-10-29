"""Behavioral tests for ``chat-stream.js`` under node: the ``#run-<id>`` link target, the progress pill's diff, and the
id a live run-status marker takes."""

from __future__ import annotations

import json
from types import SimpleNamespace

from sessions.models import RunStatus, SessionOrigin
from sessions.transcript import annotate_transcript

from tests.unit_tests.chat.chat_stream_driver import run_chat_stream
from tests.unit_tests.jsdriver import requires_node

pytestmark = requires_node

BODY = """
const rendered = new Set(payload.rendered);
const scrolled = [];
const listeners = {};
sandbox.document.addEventListener = (name, cb) => (listeners[name] ||= []).push(cb);
sandbox.document.getElementById = (id) => (rendered.has(id) ? { scrollIntoView: () => scrolled.push(id) } : null);
sandbox.window.location.hash = payload.hash;
const chat = registry.chat({ endpoint: "/api/chat", sessionLive: payload.live });
chat.$watch = () => {};
chat.$nextTick = (cb) => cb();
chat.scrollToBottom = () => scrolled.push("bottom");
chat.turns = payload.turns;
chat.init();
for (const poll of payload.polls) {
  for (const cb of listeners[poll.input] || []) cb();
  for (const id of poll.rendered) rendered.add(id);
  chat.applyPolledTurns(poll.turns, "");
}
process.stdout.write(JSON.stringify({ scrolled, anchors: chat.turns.map((t) => chat.runAnchorId(t)) }));
chat.destroy();
"""

GLOBALS = "surfaceGroup: { join: () => () => {} }, requestAnimationFrame: (cb) => cb(), cancelAnimationFrame: () => {},"


def _turn(tid: str) -> dict:
    return {"id": tid, "role": "user", "segments": [{"type": "text", "content": tid}]}


def _load(*, hash_: str, live: bool = False, rendered=(), turns=None, polls=()):
    payload = {
        "hash": hash_,
        "live": live,
        "rendered": list(rendered),
        "turns": turns if turns is not None else [_turn("h1")],
        "polls": [{"rendered": [], "input": "", **poll} for poll in polls],
    }
    return run_chat_stream(BODY, payload, extra_globals=GLOBALS)


def test_server_turns_render_their_run_anchor():
    run = SimpleNamespace(
        id="r1",
        status=RunStatus.SUCCESSFUL,
        message_id="h1",
        error_message="",
        prompt="",
        trigger_type=SessionOrigin.UI_JOB,
        created_at=None,
        finished_at=None,
        is_cancelled=False,
        notices=[],
    )
    turns = annotate_transcript([_turn("h1"), {"id": "a1", "role": "assistant", "segments": []}], [run])

    assert _load(hash_="", turns=turns)["anchors"] == ["run-r1", None]


def test_a_run_hash_outranks_the_live_sessions_scroll_to_latest():
    assert _load(hash_="#run-r1", live=True, rendered=["run-r1"])["scrolled"] == ["run-r1"]


def test_a_run_hash_outranks_the_finished_sessions_last_answer():
    assert _load(hash_="#run-r1", rendered=["run-r1"])["scrolled"] == ["run-r1"]


def test_a_run_that_renders_on_a_later_poll_is_scrolled_to_once():
    result = _load(
        hash_="#run-r2",
        live=True,
        polls=[
            {"rendered": ["run-r2"], "turns": [_turn("h1"), _turn("h2")]},
            {"turns": [_turn("h1"), _turn("h2"), _turn("h3")]},
        ],
    )

    assert result["scrolled"] == ["bottom", "run-r2", "bottom"]


def test_a_hash_that_names_no_run_keeps_the_default_scroll():
    assert _load(hash_="#tool-t1", live=True, rendered=["tool-t1"])["scrolled"] == ["bottom"]


def test_a_reader_who_takes_over_the_scroll_is_not_pulled_to_a_run_that_renders_later():
    result = _load(
        hash_="#run-r2",
        live=True,
        polls=[{"rendered": ["run-r2"], "turns": [_turn("h1"), _turn("h2")], "input": "wheel"}],
    )

    assert result["scrolled"] == ["bottom", "bottom"]


QUEUED_BODY = """
const target = { scrollIntoView: () => scrolls.push("run-r1") };
sandbox.document.getElementById = (id) => (id === "run-r1" ? target : null);
const main = {
  scrollHeight: 900, scrollTop: 0, clientHeight: 300,
  addEventListener: () => {}, removeEventListener: () => {},
  scrollTo: ({ top }) => scrolls.push(`bottom:${top}`),
};
sandbox.document.querySelector = (sel) => (sel === "main" ? main : null);
sandbox.window.location.hash = "#run-r1";
const chat = registry.chat({ endpoint: "/api/chat", sessionLive: false });
chat.$watch = () => {};
chat.$nextTick = (cb) => cb();
chat.turns = [{ id: "h1", role: "user", segments: [] }];
chat.init();
chat.scrollToBottom({ force: true });
chat.revealRunAnchor();
for (const cb of frames.splice(0)) if (cb) cb();
process.stdout.write(JSON.stringify({ scrolls, autoFollow: chat._autoFollow }));
chat.destroy();
"""

QUEUED_PRELUDE = "const frames = []; const scrolls = [];"
QUEUED_GLOBALS = (
    "surfaceGroup: { join: () => () => {} },"
    " requestAnimationFrame: (cb) => frames.push(cb), cancelAnimationFrame: (id) => { frames[id - 1] = null; },"
)


def test_revealing_a_run_cancels_a_scroll_to_bottom_already_queued():
    result = run_chat_stream(QUEUED_BODY, {}, prelude=QUEUED_PRELUDE, extra_globals=QUEUED_GLOBALS)

    assert result == {"scrolls": ["run-r1"], "autoFollow": False}


PILL_BODY = """
const chat = registry.chat({ endpoint: "/api/chat", diffInDetails: payload.diffInDetails });
chat.diffStats = { added: 48, removed: 12 };
chat.filesTouched = payload.files;
process.stdout.write(JSON.stringify(chat.progressPill));
"""


def _pill(*, diff_in_details: bool, files=()):
    return run_chat_stream(
        PILL_BODY, {"diffInDetails": diff_in_details, "files": [{"path": p} for p in files]}, extra_globals=GLOBALS
    )


def test_the_progress_pill_shows_the_diff_where_no_header_does():
    assert _pill(diff_in_details=False) == {"tone": "idle", "label": "", "diff": {"added": 48, "removed": 12}}


def test_a_session_page_takes_the_diff_and_leaves_the_pill_its_counts():
    assert _pill(diff_in_details=True, files=["a.py", "b.py"]) == {"tone": "idle", "label": "2 files"}
    assert _pill(diff_in_details=True) is None


MARKER_BODY = """
const chat = registry.chat({ endpoint: "/api/chat" });
Object.assign(chat, { scrollToBottom: () => {}, _activeRun: { threadId: "t1", runId: "client-run" } });
const turn = { id: "a-1", role: "assistant", segments: [] };
chat.turns = [turn];
for (const evt of payload.events) chat.dispatch(evt, turn, 0);
process.stdout.write(JSON.stringify(chat.turns.filter((t) => t.role === "run_status").map((t) => t.id)));
"""

RUN_FAILED = {"type": "RUN_ERROR", "code": "run_failed", "message": "Run failed."}


def test_a_live_failure_marker_takes_the_run_rows_id_once_the_server_names_it():
    events = [{"type": "CUSTOM", "name": "run_record", "value": {"id": "pk-1"}}, RUN_FAILED]

    assert run_chat_stream(MARKER_BODY, {"events": events}) == ["run-status-pk-1"]


def test_a_failure_before_the_run_row_exists_keeps_the_pages_run_id():
    assert run_chat_stream(MARKER_BODY, {"events": [RUN_FAILED]}) == ["run-status-client-run"]


NOTICE_BODY = """
const chat = registry.chat({ endpoint: "/api/chat" });
chat.$watch = () => {};
chat.$nextTick = (cb) => cb();
chat.scrollToBottom = () => {};
chat.turns = payload.turns;
chat.init();
const notice = chat.turns.find((t) => t.role === "run_notice");
const ti = chat.turns.indexOf(notice);
process.stdout.write(JSON.stringify({
  todos: chat.latestTodos.map((t) => t.content),
  files: chat.filesTouched.map((f) => f.path),
  visible: chat.isTurnVisible(notice, ti === chat.turns.length - 1),
  actions: chat.turnHasActions(notice, ti),
  copyable: chat.canCopyTurn(notice, ti),
  aborted: chat._lastRunAborted(),
}));
chat.destroy();
"""


def test_a_run_notice_renders_without_hiding_the_runs_todos_or_outputs():
    todos_args = json.dumps({"todos": [{"content": "review"}]})
    todos = {"type": "tool_call", "id": "c1", "name": "write_todos", "args": todos_args}
    write = {"type": "tool_call", "id": "c2", "name": "write_file", "args": '{"file_path": "a.py"}', "result": "ok"}
    turns = [
        _turn("h1"),
        {"id": "a1", "role": "assistant", "segments": [todos, write, {"type": "text", "content": "done"}]},
        {"id": "run-notice-r1-0", "role": "run_notice", "message": "cr-tests ran on m: x could not be used"},
    ]

    result = run_chat_stream(NOTICE_BODY, {"turns": turns}, extra_globals=GLOBALS)

    assert result == {
        "todos": ["review"],
        "files": ["a.py"],
        "visible": True,
        "actions": False,
        "copyable": False,
        "aborted": False,
    }
