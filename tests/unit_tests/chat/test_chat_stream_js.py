"""Behavioral tests for ``chat-stream.js`` under node: the ``#run-<id>`` link target."""

from __future__ import annotations

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
