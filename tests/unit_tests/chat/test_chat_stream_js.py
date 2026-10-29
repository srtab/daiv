"""Behavioral tests for ``chat-stream.js`` under node: the ``#run-<id>`` link target, the progress pill's diff and
the run blocks the transcript renders."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sessions.models import RunStatus, SessionOrigin
from sessions.transcript import annotate_transcript

from tests.unit_tests.chat.chat_stream_driver import REACTIVITY, run_chat_stream
from tests.unit_tests.chat.transcript_fixtures import asst, bash, read, text, thinking, user
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
        started_at=None,
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


DRIVE_BODY = """
const { turns, script, countBuilds, config } = payload;
let builds = 0;
if (countBuilds) {
  const build = sandbox.window.buildRunBlocks;
  sandbox.window.buildRunBlocks = (...args) => { builds++; return build(...args); };
}
const chat = reactive(registry.chat({ endpoint: "/api/chat", ...config }));
chat.$watch = watchOn(chat);
chat.$nextTick = () => {};
chat.scrollToBottom = () => {};
chat.turns = turns;
chat.streaming = turns.some((t) => t.streaming);
chat.init();
const live = () => chat.turns.findLast((t) => t.streaming) ?? chat.turns[chat.turns.length - 1];
const segment = (id) => chat.turns.flatMap((t) => t.segments || []).find((s) => s.id === id);
const running = (block) => {
  for (const item of block.items) {
    const steps = item.kind === "group" ? item.steps : item.kind === "step" ? [item.step] : [];
    const step = steps.find((s) => s.seg.status === "running");
    if (step) return step.row.livePhrase;
  }
  return null;
};
const out = [];
for (const entry of script) {
  const [op, arg, value] = entry.split(":");
  if (op === "block0") {
    const block = chat.runBlocks[0];
    const workedFor = chat.workedForLabel(block);
    out.push({ stats: block.stats, running: running(block), folded: block.folded, workedFor });
  }
  if (op === "landResult") Object.assign(segment(arg), { result: "1  x", status: "done" });
  if (op === "pushText") live().segments.push({ type: "text", content: "Found it." });
  if (op === "startedAgo") chat.runStartedAt = Date.now() - Number(arg) * 1000;
  if (op === "finish") chat._finishTurn(live(), arg);
  if (op === "dropReplayedThinking") {
    chat._reasoningIndex.set("replayed", 0);
    chat._dropReplayedThinking(live());
  }
  if (op === "fillReplayedResult") chat._fillReplayedResult({ toolCallId: arg, content: "1  x" });
  if (op === "open") chat.setOpen(arg, true);
  if (op === "isOpen") out.push(chat.isOpen(arg));
  if (op === "toggle") chat.setOpen(arg, value === "true", arg === chat.currentGroupKey);
  if (op === "shown") out.push(chat.isOpen(arg, arg === chat.currentGroupKey));
  if (op === "recorded") out.push(chat.openKeys[arg] ?? null);
  if (op === "currentGroupKey") out.push(chat.currentGroupKey);
  if (op === "runStatus") out.push(chat.runStatus.label);
  if (op === "readBlocks") chat.runBlocks.length;
}
process.stdout.write(JSON.stringify(countBuilds ? builds : out));
chat.destroy();
"""


def _drive(turns, *, script, count_builds=False, config=None):
    payload = {"turns": turns, "script": script, "countBuilds": count_builds, "config": config or {}}
    return run_chat_stream(DRIVE_BODY, payload, prelude=REACTIVITY, extra_globals=GLOBALS)


LIVE = [user("u1"), asst("a1", read("c1", result=None, status="running"), streaming=True)]


def test_run_blocks_rederive_when_a_result_lands():
    out = _drive(LIVE, script=["block0", "landResult:c1", "block0"])
    assert out[0]["stats"]["steps"] == out[1]["stats"]["steps"] == 1
    assert out[0]["running"] == "Reading x" and out[1]["running"] is None


def test_opened_step_stays_open_after_replay_mutates_segments():
    turns = [user("u1"), asst("a1", thinking(), read("c1", result=None, status="running"), streaming=True)]
    out = _drive(turns, script=["open:s-c1", "dropReplayedThinking", "fillReplayedResult:c1", "isOpen:s-c1"])
    assert out[-1] is True


def test_finishing_a_turn_folds_the_run_with_a_duration():
    out = _drive(LIVE, script=["landResult:c1", "finish:finished", "block0"])
    assert out[-1]["folded"] is True and out[-1]["workedFor"].startswith("Worked for ")


def test_finishing_a_turn_stamps_the_run_the_stream_left_untimed():
    turns = [
        user("u1", started_at=None),
        asst("a1", read("c1", result=None, status="running"), streaming=True, received_at=None),
    ]
    out = _drive(turns, script=["startedAgo:5", "landResult:c1", "finish:finished", "block0"])
    assert out[-1]["workedFor"] == "Worked for 5s · 1 step"


def test_a_run_without_timestamps_folds_behind_its_step_count():
    turns = [user("u1", started_at=None), asst("a1", read("c1"), read("c2"), text("Done."))]
    assert _drive(turns, script=["block0"])[0]["workedFor"] == "2 steps"


def test_working_line_names_the_running_step():
    turns = [user("u1"), asst("a1", bash("c1", status="running"), streaming=True)]
    assert _drive(turns, script=["runStatus"])[-1] == "Running pytest…"


@pytest.mark.parametrize(
    ("segment", "label"),
    [
        (read("c1"), "Thinking…"),
        (thinking(status="running"), "Thinking…"),
        (
            {"type": "publish_phase", "id": "p1", "label": "Committing changes", "status": "running"},
            "Committing changes…",
        ),
    ],
)
def test_working_line_between_steps_and_while_publishing(segment, label):
    turns = [user("u1"), asst("a1", text("Done."), segment, streaming=True)]
    assert _drive(turns, script=["runStatus"])[-1] == label


def test_blocks_are_built_once_per_change_not_per_read():
    turns = [user("u1"), asst("a1", *[read(f"c{i}") for i in range(500)])]
    calls = _drive(turns, script=["readBlocks", "readBlocks", "readBlocks"], count_builds=True)
    assert calls == 1


LIVE_GROUP = [user("u1"), asst("a1", read("c1"), read("c2"), streaming=True)]


def test_the_live_group_stays_current_until_text_arrives():
    assert _drive(LIVE_GROUP, script=["currentGroupKey", "pushText", "currentGroupKey"]) == ["g-s-c1", None]


def test_an_auto_opened_group_records_only_what_the_user_changed():
    opened = _drive(LIVE_GROUP, script=["toggle:g-s-c1:true", "recorded:g-s-c1", "pushText", "shown:g-s-c1"])
    closed = _drive(LIVE_GROUP, script=["toggle:g-s-c1:false", "recorded:g-s-c1", "pushText", "shown:g-s-c1"])
    assert opened == [None, False]
    assert closed == [False, False]
