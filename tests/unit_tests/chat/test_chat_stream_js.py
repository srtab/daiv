"""Behavioral tests for ``chat-stream.js`` under node: the ``#run-<id>`` link target, the progress pill's diff, the
run blocks the transcript renders, the id a live run-status marker takes and the context a tool body gets."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sessions.models import RunStatus, SessionOrigin
from sessions.transcript import annotate_transcript

from automation.agent.events import PUBLISH_RESULT_EVENT, publish_result_payload
from tests.unit_tests.chat.chat_stream_driver import REACTIVITY, run_chat_stream
from tests.unit_tests.chat.transcript_fixtures import (
    asst,
    bash,
    delegate,
    question,
    read,
    refused_question,
    text,
    thinking,
    tool_call,
    user,
)
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
        publish_status="",
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
let broken = Boolean(payload.broken);
const errors = [];
sandbox.console.error = (...args) => errors.push(args.map(String).join(" "));
const build = sandbox.window.buildRunBlocks;
sandbox.window.buildRunBlocks = (...args) => {
  builds++;
  if (broken) throw new Error("a renderer choked");
  return build(...args);
};
const chat = reactive(registry.chat({ endpoint: "/api/chat", ...config }));
const watched = [];
chat.$watch = (expr, cb) => { watched.push(expr); return watchOn(chat)(expr, cb); };
chat.$nextTick = () => {};
chat.scrollToBottom = () => {};
chat.turns = turns;
chat.streaming = turns.some((t) => t.streaming);
chat.init();
const live = () => chat.turns.findLast((t) => t.streaming) ?? chat.turns[chat.turns.length - 1];
const segment = (id) => chat.turns.flatMap((t) => t.segments || []).find((s) => s.id === id);
const stepsOf = (item) => (item.kind === "group" ? item.steps : item.kind === "step" ? [item] : []);
const running = (block) => {
  for (const item of block.items) {
    const step = stepsOf(item).find((s) => s.seg.status === "running");
    if (step) return step.row.livePhrase;
  }
  return null;
};
const stepFor = (id) => {
  for (const block of chat.runBlocks) {
    for (const item of block.items) {
      const step = stepsOf(item).find((s) => s.seg.id === id);
      if (step) return { kind: item.kind, key: step.key };
    }
  }
  return null;
};
const itemsOf = (block) => [...block.items, ...block.endingText, ...block.cards, ...block.endingTail];
let snapshot = [];
const out = [];
for (const entry of script) {
  const [op, arg, value] = entry.split(":");
  if (op === "block0" || op === "block") {
    const block = chat.runBlocks[op === "block" ? Number(arg) : 0];
    const workedFor = chat.workedForLabel(block);
    out.push({ stats: block.stats, running: running(block), folded: block.folded, workedFor });
  }
  if (op === "landResult") Object.assign(segment(arg), { result: "1  x", status: "done" });
  if (op === "argsDelta") segment(arg).args = JSON.stringify({ file_path: "/workspace/repo/y" });
  if (op === "pushText") live().segments.push({ type: "text", content: "Found it." });
  if (op === "token") live().segments.findLast((s) => s.type === "text").content += " more";
  if (op === "startedAgo") chat.runStartedAt = Date.now() - Number(arg) * 1000;
  if (op === "finish") chat._finishTurn(live(), arg);
  if (op === "poll") chat.applyPolledTurns(JSON.parse(JSON.stringify(chat.turns)), "", arg === "true");
  if (op === "dropReplayedThinking") {
    chat._reasoningIndex.set("replayed", 0);
    chat._dropReplayedThinking(live());
  }
  if (op === "fillReplayedResult") chat._fillReplayedResult({ toolCallId: arg, content: "1  x" });
  if (op === "openStep") chat.setOpen(stepFor(arg).key, true);
  if (op === "jump") chat.jumpToTool(`tool-${arg}`);
  if (op === "stepRows") {
    const block = chat.runBlocks[0];
    const rows = (item) => [item.kind, chat.stepRowsOf(item).map((s) => s.key)];
    out.push([...block.items, ...block.endingText, ...block.endingTail].map(rows));
  }
  if (op === "stepOpen") {
    const step = stepFor(arg);
    out.push({ kind: step.kind, open: chat.isOpen(step.key) });
  }
  if (op === "toggle") chat.setOpen(arg, value === "true", arg === chat.currentGroupKey);
  if (op === "shown") out.push(chat.isOpen(arg, arg === chat.currentGroupKey));
  if (op === "recorded") out.push(chat.openKeys[arg] ?? null);
  if (op === "currentGroupKey") out.push(chat.currentGroupKey);
  if (op === "runStatus") out.push(chat.runStatus.label);
  if (op === "runActions") out.push(chat.runBlocks.map((block) => chat.runHasActions(block)));
  if (op === "readBlocks") chat.runBlocks.length;
  if (op === "blockCount") out.push(chat.runBlocks.length);
  if (op === "snapshot") snapshot = chat.runBlocks.map((block) => ({ block, items: itemsOf(block) }));
  if (op === "kept") {
    out.push(chat.runBlocks.map((block, i) =>
      block === snapshot[i].block && itemsOf(block).every((item, j) => item === snapshot[i].items[j])));
  }
  if (op === "liveText") {
    const last = chat.runBlocks[chat.runBlocks.length - 1];
    out.push(last.items.filter((item) => item.kind === "text").map((item) => item.seg.content));
  }
  if (op === "activeRun") chat._activeRun = { threadId: "t1", runId: "client-run", recordId: arg || undefined };
  if (op === "toolStart") {
    chat.dispatch({ type: "TOOL_CALL_START", toolCallId: `t-${arg}`, toolCallName: arg }, live(), 0);
  }
  if (op === "toolEnd") chat.dispatch({ type: "TOOL_CALL_END", toolCallId: `t-${arg}` }, live(), 0);
  if (op === "toolResult") {
    chat.dispatch({ type: "TOOL_CALL_RESULT", toolCallId: `t-${arg}`, content: "{}" }, live(), 0);
  }
  if (op === "publishPhase") out.push(chat.publishPhase);
  if (op === "custom") {
    const value = JSON.parse(entry.slice(op.length + arg.length + 2));
    chat.dispatch({ type: "CUSTOM", name: arg, value }, live(), 0);
  }
  if (op === "runError") chat.dispatch({ type: "RUN_ERROR", code: arg, message: "boom" }, live(), 0);
  if (op === "turnRoles") out.push(chat.turns.map((t) => t.role));
  if (op === "markers") out.push(chat.turns.filter((t) => t.role === "run_status").map((t) => t.status));
  if (op === "segmentTypes") out.push(live().segments.map((s) => s.type));
  if (op === "segmentStatuses") out.push(live().segments.map((s) => s.status));
  if (op === "block0.publish") out.push(chat.runBlocks[0].publish);
  if (op === "break") broken = true;
  if (op === "errors") out.push(errors.length);
  if (op === "watched") out.push(watched);
}
process.stdout.write(JSON.stringify(countBuilds ? builds : out));
chat.destroy();
"""


def _drive(turns, *, script, count_builds=False, config=None, broken=False):
    payload = {"turns": turns, "script": script, "countBuilds": count_builds, "config": config or {}, "broken": broken}
    return run_chat_stream(DRIVE_BODY, payload, prelude=REACTIVITY, extra_globals=GLOBALS)


LIVE = [user("u1"), asst("a1", read("c1", result=None, status="running"), streaming=True)]


def test_run_blocks_rederive_when_a_result_lands():
    out = _drive(LIVE, script=["block0", "landResult:c1", "block0"])
    assert out[0]["stats"]["steps"] == out[1]["stats"]["steps"] == 1
    assert out[0]["running"] == "Reading x" and out[1]["running"] is None


def test_opened_step_stays_open_after_replay_mutates_segments():
    turns = [user("u1"), asst("a1", thinking(), read("c1", result=None, status="running"), streaming=True)]
    script = ["openStep:c1", "stepOpen:c1", "dropReplayedThinking", "fillReplayedResult:c1", "stepOpen:c1"]
    assert _drive(turns, script=script) == [{"kind": "group", "open": True}, {"kind": "step", "open": True}]


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


def test_a_run_that_ends_before_it_starts_folds_behind_its_step_count():
    turns = [user("u1", started_at="2026-10-29T10:05:00+00:00"), asst("a1", read("c1"), read("c2"), text("Done."))]
    assert _drive(turns, script=["block0"])[0]["workedFor"] == "2 steps"


def test_working_line_names_the_running_step():
    turns = [user("u1"), asst("a1", bash("c1", status="running"), streaming=True)]
    assert _drive(turns, script=["runStatus"])[-1] == "Running pytest…"


@pytest.mark.parametrize(("segment", "label"), [(read("c1"), "Thinking…"), (thinking(status="running"), "Thinking…")])
def test_working_line_between_steps(segment, label):
    turns = [user("u1"), asst("a1", text("Done."), segment, streaming=True)]
    assert _drive(turns, script=["runStatus"])[-1] == label


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("publish_artifact", "Publishing…"),
        ("delegate_jobs", "Delegating jobs…"),
        ("ask_user_question", "Asking a question…"),
    ],
)
def test_working_line_names_a_running_call_that_is_not_a_step(name, label):
    running = tool_call("x1", name, {}, result=None, status="running")
    turns = [user("u1"), asst("a1", text("Done."), running, streaming=True)]
    assert _drive(turns, script=["runStatus"])[-1] == label


def test_a_run_whose_answer_precedes_a_tool_only_turn_still_offers_its_copy_button():
    turns = [
        user("u1"),
        asst("a1", text("Final answer"), bash("c1"), received_at=None),
        asst("a2", read("c2"), received_at=None),
    ]
    assert _drive(turns, script=["runActions"]) == [[True]]


def test_blocks_are_built_once_per_change_not_per_read():
    turns = [user("u1"), asst("a1", *[read(f"c{i}") for i in range(500)])]
    script = ["readBlocks", "readBlocks", "readBlocks", "pushText", "readBlocks", "readBlocks"]
    calls = _drive(turns, script=script, count_builds=True)
    assert calls == 2


LIVE_GROUP = [user("u1"), asst("a1", read("c1"), read("c2"), streaming=True)]


def test_the_live_group_stays_current_until_text_arrives():
    assert _drive(LIVE_GROUP, script=["currentGroupKey", "pushText", "currentGroupKey"]) == ["g-s-c1", None]


def test_an_auto_opened_group_records_only_what_the_user_changed():
    opened = _drive(LIVE_GROUP, script=["toggle:g-s-c1:true", "recorded:g-s-c1", "pushText", "shown:g-s-c1"])
    closed = _drive(LIVE_GROUP, script=["toggle:g-s-c1:false", "recorded:g-s-c1", "pushText", "shown:g-s-c1"])
    assert opened == [None, False]
    assert closed == [False, False]


TWO_RUNS = [
    user("u1"),
    asst("a1", read("c1"), read("c2"), text("Done.")),
    user("u2"),
    asst("a2", text("Looking"), read("c3", result=None, status="running"), streaming=True),
]


def test_streamed_tokens_and_args_leave_a_finished_run_untouched():
    script = ["snapshot", "token", "token", "token", "kept", "liveText", "argsDelta:c3", "kept", "block:1"]
    kept_after_tokens, live_text, kept_after_args, live = _drive(TWO_RUNS, script=script)
    assert kept_after_tokens == [True, True]
    assert live_text == ["Looking more more more"]
    assert kept_after_args == [True, False]
    assert live["running"] == "Reading y"


def test_the_poll_keeps_the_last_run_live_while_the_session_runs():
    turns = [user("u1"), asst("a1", read("c1"), text("Done."))]
    out = _drive(turns, script=["poll:true", "block0", "poll:false", "block0"])
    assert [out[0]["folded"], out[1]["folded"]] == [False, True]


def test_a_derive_that_throws_keeps_the_last_blocks():
    assert _drive(LIVE, script=["snapshot", "break", "argsDelta:c1", "kept", "errors"]) == [[True], 1]


def test_a_first_derive_that_throws_does_not_abort_init():
    blocks, errors, watched = _drive(LIVE, script=["blockCount", "errors", "watched"], broken=True)
    assert (blocks, errors) == (0, 1)
    assert "streaming" in watched


EVENT_SOURCE = """
let opened = null;
class FakeEventSource {
  static CLOSED = 2;
  constructor() { this.readyState = 1; this.listeners = {}; opened = this; }
  addEventListener(name, cb) { this.listeners[name] = cb; }
  close() { this.readyState = 2; }
}
"""

RUN_FRAMES = [
    {"type": "TOOL_CALL_START", "toolCallId": "c1", "toolCallName": "read_file"},
    {"type": "TOOL_CALL_ARGS", "toolCallId": "c1", "delta": '{"file_path": "/workspace/repo/x"}'},
    {"type": "TOOL_CALL_END", "toolCallId": "c1"},
    {"type": "TOOL_CALL_RESULT", "toolCallId": "c1", "content": "1  x"},
    {"type": "TEXT_MESSAGE_START", "messageId": "m1"},
    {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "Done."},
]

LIVE_PAGE_BODY = """
const chat = reactive(registry.chat({
  endpoint: "/api/chat",
  streamEndpoint: "/api/chat/stream",
  sessionLive: true,
  activeRunId: "r1",
  activeRunStartedAt: new Date(Date.now() - 5000).toISOString(),
  thread: { thread_id: "t1", repo_id: "r", ref: "main" },
}));
chat.$watch = watchOn(chat);
chat.$nextTick = () => {};
chat.scrollToBottom = () => {};
chat.autosize = () => {};
chat.turns = payload.turns;
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const play = () => {
  for (const evt of payload.frames) opened.onmessage({ data: JSON.stringify(evt) });
  opened.listeners.end({ data: JSON.stringify({ reason: "finished" }) });
};
const fold = (block) => ({ folded: block.folded, workedFor: chat.workedForLabel(block) });

chat.init();
play();
await tick();
const rejoined = fold(chat.runBlocks[0]);

chat.draftMessage = "again";
const sent = chat.submit();
await tick();
play();
await sent;
process.stdout.write(JSON.stringify({ rejoined, later: fold(chat.runBlocks[1]) }));
chat.destroy();
"""


def test_a_page_loaded_live_folds_each_run_as_its_stream_finishes():
    out = run_chat_stream(
        LIVE_PAGE_BODY,
        {"turns": [user("u1", started_at=None)], "frames": RUN_FRAMES},
        prelude=REACTIVITY + EVENT_SOURCE,
        extra_globals=GLOBALS
        + " EventSource: FakeEventSource, setTimeout, fetch: async () => ({ ok: true, json: async () => ({}) }),",
    )
    assert out["rejoined"] == {"folded": True, "workedFor": "Worked for 5s · 1 step"}
    assert out["later"]["folded"] is True
    assert out["later"]["workedFor"].startswith("Worked for ")


def test_a_lone_step_a_special_call_with_a_row_and_a_refused_question_render_as_step_rows():
    segs = [read("c1"), text("x"), read("c2"), read("c3"), delegate("d1"), refused_question("q1")]
    segs.append(question("q2"))
    rows = _drive([user("u1"), asst("a1", *segs, streaming=True)], script=["stepRows"])[0]
    assert rows == [
        ["step", ["s-c1"]],
        ["text", []],
        ["group", []],
        ["special", ["x-d1"]],
        ["question", ["q-q1"]],
        ["question", []],
    ]


def test_jumping_to_a_folded_step_opens_its_fold_group_and_row():
    turns = [user("u1"), asst("a1", read("c1"), read("c2"), text("Done."))]
    assert _drive(turns, script=["jump:c2", "shown:u1", "shown:g-s-c1", "stepOpen:c2"]) == [
        True,
        True,
        {"kind": "group", "open": True},
    ]


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


SKILL_LINKS_BODY = """
const chat = registry.chat({ endpoint: "/api/chat" });
chat.turns = payload.turns;
chat.applySkillLinks(payload.server);
process.stdout.write(JSON.stringify(chat.turns[1].segments.map((seg) => seg.skill_url ?? null)));
"""


def test_a_streamed_skill_call_takes_its_link_from_the_turns_fetched_after_the_run():
    skill = tool_call("s1", "skill", {"skill": "plan"}, result="body")
    other = tool_call("s2", "skill", {"skill": "mine"}, result="body")
    server = [user("u1"), asst("a1", {**skill, "skill_url": "/dashboard/skills/plan/"}, other)]
    payload = {"turns": [user("u1"), asst("a1", skill, other)], "server": server}
    assert run_chat_stream(SKILL_LINKS_BODY, payload, extra_globals=GLOBALS) == ["/dashboard/skills/plan/", None]


CTX_BODY = """
const chat = registry.chat({ endpoint: "/api/chat" });
chat.mcpCatalog = payload.catalog;
chat.thread = { merge_request: payload.mr };
let seen = null;
sandbox.window.toolBodyHTML = (seg, ctx) => {
  seen = { ...ctx, seg: seg.id, latestArtifactsById: ctx.latestArtifactsById === chat.latestArtifactsById };
  return "";
};
chat.toolBodyHTML({ id: "c1" });
process.stdout.write(JSON.stringify(seen));
"""


MR_URL = "https://g/x/-/merge_requests/1"


@pytest.mark.parametrize("mr,url", [({"url": MR_URL}, MR_URL), (None, None)])
def test_a_tool_body_gets_the_servers_the_artifacts_and_the_merge_request(mr, url):
    catalog = [{"name": "sentry", "read_only_tools": ["search_issues"], "icon": ""}]
    seen = run_chat_stream(CTX_BODY, {"catalog": catalog, "mr": mr}, extra_globals=GLOBALS)
    assert seen == {
        "seg": "c1",
        "mcpServers": ["sentry"],
        "mcpRows": catalog,
        "latestArtifactsById": True,
        "mergeRequestUrl": url,
    }


ROWS_BODY = """
const seen = [];
const build = sandbox.window.buildRunBlocks;
sandbox.window.buildRunBlocks = (turns, opts) => {
  seen.push({ servers: opts.mcpServers, rows: opts.mcpRows });
  return build(turns, opts);
};
const chat = reactive(registry.chat({ endpoint: "/api/chat" }));
chat.mcpCatalog = payload.catalog;
chat.$watch = (expr, cb) => watchOn(chat)(expr, cb);
chat.$nextTick = () => {};
chat.scrollToBottom = () => {};
chat.turns = payload.turns;
chat.init();
process.stdout.write(JSON.stringify(seen[seen.length - 1]));
chat.destroy();
"""


def test_the_run_blocks_get_the_mcp_server_rows():
    catalog = [{"name": "sentry", "read_only_tools": ["search_issues"], "icon": "https://x.test/a.png"}]
    payload = {"catalog": catalog, "turns": [user("u1"), asst("a1", read("c1"))]}
    seen = run_chat_stream(ROWS_BODY, payload, prelude=REACTIVITY, extra_globals=GLOBALS)
    assert seen == {"servers": ["sentry"], "rows": catalog}


MR = SimpleNamespace(merge_request_id=7, web_url="https://git.example/mr/7")
STATS = SimpleNamespace(files_changed=3, lines_added=48, lines_removed=12)
PUBLISHING = [user("u1"), asst("a1", text("Done."), streaming=True)]


def _publish_custom(**kwargs) -> str:
    return f"custom:{PUBLISH_RESULT_EVENT}:{json.dumps(publish_result_payload(**kwargs))}"


def test_publish_phase_names_the_working_line_and_never_renders_a_segment():
    out = _drive(PUBLISHING, script=["toolStart:PullRequestMetadata", "runStatus", "segmentTypes"])
    assert out[0] == "Creating merge request…" and "publish_phase" not in out[1]


def test_committing_phase_names_the_working_line():
    assert _drive(PUBLISHING, script=["toolStart:CommitMetadata", "runStatus"]) == ["Committing changes…"]


def test_phase_outlasts_its_call_until_the_result_lands():
    script = ["toolStart:CommitMetadata", "toolEnd:CommitMetadata", "toolResult:CommitMetadata", "runStatus"]
    out = _drive(PUBLISHING, script=[*script, _publish_custom(status="pushed"), "runStatus"])
    assert out == ["Committing changes…", "Thinking…"]


@pytest.mark.parametrize(
    ("first", "second"),
    [("PullRequestMetadata", "CommitMetadata"), ("CommitMetadata", "PullRequestMetadata")],
    ids=["mr-first", "commit-first"],
)
def test_parallel_metadata_calls_name_the_merge_request_step_until_the_result_lands(first, second):
    script = [f"toolStart:{first}", f"toolStart:{second}", f"toolEnd:{first}", "runStatus"]
    script += [f"toolEnd:{second}", "runStatus", _publish_custom(status="opened"), "runStatus"]
    out = _drive(PUBLISHING, script=script)
    assert out == ["Creating merge request…", "Creating merge request…", "Thinking…"]


def test_phase_clears_when_the_turn_finishes_without_an_event():
    script = ["activeRun", "toolStart:CommitMetadata", "publishPhase", "finish:finished", "publishPhase"]
    assert _drive(PUBLISHING, script=script) == ["Committing changes", ""]


def test_publish_event_adds_the_result_to_the_run():
    kwargs = {"status": "opened", "merge_request": MR, "diff_stats": STATS}
    out = _drive(PUBLISHING, script=[_publish_custom(**kwargs), "finish:finished", "block0.publish"])
    assert out[-1] == [
        {
            "status": "opened",
            "merge_request_iid": 7,
            "merge_request_web_url": "https://git.example/mr/7",
            "files_changed": 3,
            "lines_added": 48,
            "lines_removed": 12,
            "error": "",
        }
    ]


def test_failed_publish_carries_the_kinds_fixed_message_and_the_retry_anchor():
    script = ["activeRun:pk-1", _publish_custom(status="failed", error_kind="push_rejected"), "block0.publish"]
    (entry,) = _drive(PUBLISHING, script=script)[-1]
    assert entry["id"] == "run-status-pk-1"
    assert (entry["status"], entry["error"]) == ("failed", "The push was rejected")


def test_the_live_failed_entry_takes_the_page_run_id_before_the_server_names_the_row():
    script = ["activeRun", _publish_custom(status="failed", error_kind="unknown"), "block0.publish"]
    assert _drive(PUBLISHING, script=script)[-1][0]["id"] == "run-status-client-run"


@pytest.mark.parametrize(
    ("status", "kind", "message"),
    [
        ("not_published", "conflicts", "Merge conflicts are left in the changed files"),
        ("pushed", "mr_pending", "The merge request isn't created yet"),
    ],
)
def test_live_entry_shows_the_kinds_label_as_a_reload_does(status, kind, message):
    script = [_publish_custom(status=status, error_kind=kind), "block0.publish"]
    assert _drive(PUBLISHING, script=script)[-1][0]["error"] == message


def test_failed_publish_suppresses_the_generic_failure_marker():
    script = [
        "activeRun",
        _publish_custom(status="failed", error_kind="push_rejected"),
        "runError:run_failed",
        "finish:finished",
        "turnRoles",
    ]
    assert "run_status" not in _drive(PUBLISHING, script=script)[-1]


@pytest.mark.parametrize("code", ["run_cancelled", "run_interrupted"])
def test_an_aborted_run_keeps_its_marker_despite_a_failed_publish(code):
    script = ["activeRun", _publish_custom(status="failed", error_kind="unknown"), f"runError:{code}", "markers"]
    assert _drive(PUBLISHING, script=script)[-1] == ["aborted"]


def test_a_run_error_without_a_failed_publish_still_pushes_its_marker():
    assert _drive(PUBLISHING, script=["activeRun", "runError:run_failed", "markers"])[-1] == ["failed"]


def test_a_fallback_ending_does_not_stack_a_marker_on_the_red_line():
    running = tool_call("x1", "bash", {}, result=None, status="running")
    turns = [user("u1"), asst("a1", running, streaming=True)]
    script = [
        "activeRun",
        _publish_custom(status="failed", error_kind="unknown"),
        "finish:connection_lost",
        "markers",
        "segmentStatuses",
    ]
    out = _drive(turns, script=script)
    assert out[-2] == [] and out[-1] == ["error"]


QUEUED_MESSAGE_BODY = """
const chat = reactive(registry.chat({
  endpoint: "/api/chat",
  streamEndpoint: "/api/chat/stream",
  thread: { thread_id: "t1", repo_id: "r", ref: "main" },
}));
chat.$watch = watchOn(chat);
chat.$nextTick = (cb) => cb && cb();
chat.scrollToBottom = () => {};
chat.autosize = () => {};
chat.init();
chat.draftMessage = "Also add a test";
await chat.submit();
const queued = {
  turns: chat.turns.map((t) => ({ role: t.role, pending: Boolean(t.pending) })),
  streaming: chat.streaming,
  live: chat.sessionLive,
  queuedRunId: chat.queuedRunId,
  ended: dispatched.filter((e) => e.type === "daiv:live-run-ended").length,
  opened: opened !== null,
};
chat.followLiveRun({ id: "next-run", stoppable: true, started_at: new Date().toISOString() });
const following = { streaming: chat.streaming, run: chat._activeRun?.runId, queuedRunId: chat.queuedRunId };
process.stdout.write(JSON.stringify({ queued, following }));
chat.destroy();
"""


def test_a_message_sent_while_the_session_is_busy_shows_as_queued_until_its_run_starts():
    reply = {"run_id": "r1", "thread_id": "t1", "queued": {"event_id": "e1", "run_id": "next-run", "position": "next"}}
    fetch = f"fetch: async () => ({{ ok: true, json: async () => ({json.dumps(reply)}) }}),"

    out = run_chat_stream(
        QUEUED_MESSAGE_BODY,
        {},
        prelude=REACTIVITY + EVENT_SOURCE,
        extra_globals=GLOBALS + " EventSource: FakeEventSource, setTimeout, " + fetch,
    )

    assert out["queued"] == {
        "turns": [{"role": "user", "pending": True}],
        "streaming": False,
        "live": True,
        "queuedRunId": "next-run",
        "ended": 0,
        "opened": False,
    }
    assert out["following"] == {"streaming": True, "run": "next-run", "queuedRunId": ""}
