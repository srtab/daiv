"""Behavioral tests for ``buildRunBlocks`` in ``transcript-blocks.js``, under node."""

from __future__ import annotations

import pytest

from tests.unit_tests.chat.transcript_driver import run_transcript
from tests.unit_tests.chat.transcript_fixtures import (
    ENDED,
    asst,
    bash,
    delegate,
    edit,
    grep,
    publish,
    question,
    read,
    refused_question,
    status_turn,
    text,
    thinking,
    tool_call,
    user,
)
from tests.unit_tests.jsdriver import requires_node

pytestmark = requires_node

BUILD = "buildRunBlocks(vars.turns, {inFlight: vars.inFlight, latestArtifactsById: new Map(), mcpRows: vars.servers})"

KEYS = """((blocks) => {
  const keys = [];
  const add = (items) => items.forEach((item) => {
    keys.push(item.key);
    if (item.kind === "group") item.steps.forEach((step) => keys.push(step.key));
  });
  blocks.forEach((b) => { keys.push(b.key); add(b.items); add(b.endingText); add(b.cards); add(b.endingTail); });
  return keys;
})"""


def _blocks(turns, *, in_flight=False, servers=()):
    rows = [{"name": name} for name in servers]
    return run_transcript(BUILD, {"turns": turns, "inFlight": in_flight, "servers": rows})


def _group_line(segs, servers=()):
    (group,) = _blocks([user("u1"), asst("a1", *segs)], in_flight=True, servers=servers)[0]["items"]
    return group["line"]


def _keys(turns):
    return run_transcript(f"{KEYS}({BUILD})", {"turns": turns, "inFlight": False, "servers": []})


def _kinds(items):
    return [i["kind"] for i in items]


def check_jobs(call_id):
    return tool_call(call_id, "check_delegated_jobs", {"batch_id": "b1"}, result='{"jobs": []}')


def timed_thought(seg_id, ms):
    return {"type": "thinking", "id": seg_id, "content": "x", "status": "done", "startedAt": 1000, "endedAt": 1000 + ms}


def test_text_splits_steps_into_groups_and_a_lone_step_is_not_wrapped():
    turns = [user("u1"), asst("a1", read("c1"), text("x"), read("c2"), bash("c3"), thinking())]
    blocks = _blocks(turns, in_flight=True)
    assert [i["kind"] for i in blocks[0]["items"]] == ["step", "text", "group"]


def test_finished_run_folds_and_ending_is_everything_after_the_last_step():
    blocks = _blocks([user("u1"), asst("a1", read("c1"), read("c2"), text("answer"))])
    b = blocks[0]
    assert b["folded"] and _kinds(b["endingText"]) == ["text"] and b["endingTail"] == [] and b["stats"]["steps"] == 2


def test_live_run_does_not_fold():
    assert _blocks([user("u1"), asst("a1", read("c1"), read("c2"), streaming=True)])[0]["folded"] is False


def test_special_calls_are_not_steps():
    blocks = _blocks([user("u1"), asst("a1", read("c1"), publish("p1"), read("c2"), question("q1"))])
    assert blocks[0]["stats"]["steps"] == 2 and blocks[0]["endingTail"][-1]["kind"] == "question"


def test_folded_run_hoists_its_artifacts_below_the_ending():
    b = _blocks([user("u1"), asst("a1", read("c1"), publish("p1"), read("c2"), text("done"))])[0]
    assert [i["kind"] for i in b["items"]] == ["step", "special", "step"]
    assert [i["kind"] for i in b["cards"]] == ["artifacts"]


def test_failed_and_stopped_runs_fold_with_the_marker_after():
    b = _blocks([user("u1"), asst("a1", read("c1"), grep("c2", status="error")), status_turn("failed")])[0]
    assert b["folded"] and b["stats"]["failed"] == 1 and len(b["statuses"]) == 1


def test_group_line_counts_failures_and_sums_line_counts():
    line = _group_line([edit("c1", "a\n", "b\nc\n"), bash("c2", exit_code=1)])
    assert (line["text"], line["failed"], line["added"], line["removed"]) == ("Edited 1 file, ran 1 command", 1, 2, 1)


def test_keys_are_unique_and_stable_without_segment_ids():
    turns = [user("u1"), asst("a1", thinking(), thinking(), text("x"))]
    first, second = _keys(turns), _keys(turns)
    assert first == second and len(first) == len(set(first))


def test_legacy_turns_without_timestamps_or_results_build():
    b = _blocks([user("u1", started_at=None), asst("a1", read("c1", result=None, status="error"))])[0]
    assert b["startedAt"] is None and b["folded"] is True


def test_pending_prompt_is_its_own_block_without_items():
    blocks = _blocks([user("u1"), asst("a1", text("ok")), user("u2", pending=True)])
    assert blocks[-1]["anchor"]["pending"] and blocks[-1]["items"] == []


def test_a_publish_after_the_last_step_shows_only_in_the_cards_and_the_ending_splits_text_from_the_rest():
    b = _blocks([user("u1"), asst("a1", read("c1"), read("c2"), publish("p1"), delegate("d1"), text("done"))])[0]
    assert _kinds(b["endingText"]) == ["text"]
    assert [i["seg"]["id"] for i in b["endingTail"]] == ["d1"]
    assert [len(card["items"]) for card in b["cards"]] == [1]


def test_a_folded_runs_closing_question_comes_after_its_text_and_its_cards():
    b = _blocks([user("u1"), asst("a1", read("c1"), publish("p1"), text("Written up."), question("q1"))])[0]
    assert (_kinds(b["endingText"]), _kinds(b["cards"]), _kinds(b["endingTail"])) == (
        ["text"],
        ["artifacts"],
        ["question"],
    )


def test_a_publish_inside_the_fold_is_a_published_row():
    b = _blocks([user("u1"), asst("a1", read("c1"), publish("p1"), read("c2"))])[0]
    assert b["items"][1]["row"]["phrase"] == "Published"


def test_live_run_keeps_its_publishes_in_place_as_one_card():
    b = _blocks([user("u1"), asst("a1", read("c1"), publish("p1"), publish("p2"), read("c2"), streaming=True)])[0]
    assert _kinds(b["items"]) == ["step", "artifacts", "step"]
    assert [it["id"] for it in b["items"][1]["items"]] == ["art-p1", "art-p2"]
    assert b["cards"] == [] and b["endingText"] == [] and b["endingTail"] == []


def test_checking_delegated_jobs_is_a_step_and_delegating_is_not():
    b = _blocks([user("u1"), asst("a1", read("c1"), check_jobs("j1"), delegate("d1"), read("c2"))], in_flight=True)[0]
    assert _kinds(b["items"]) == ["group", "special", "step"]
    assert b["items"][0]["line"]["text"] == "Read 1 file, checked delegated jobs"
    assert b["stats"]["steps"] == 3


def test_a_block_exposes_the_publish_list_of_its_last_assistant_turn():
    entry = {"status": "opened", "merge_request_iid": 7}
    stamped = {**asst("a2", text("Done.")), "publish": [entry]}
    blocks = _blocks([user("u1"), asst("a1", read("c1")), stamped, user("u2"), asst("a3", text("Hi"))])
    assert [b["publish"] for b in blocks] == [[entry], []]


def test_a_publish_list_survives_a_later_turn_that_joins_its_run():
    entry = {"status": "failed", "id": "run-status-r1"}
    carrier = {**asst("r1-publish"), "publish": [entry]}
    (block,) = _blocks([user("u1"), carrier, asst("r1-artifacts", publish("p1"))])
    assert block["publish"] == [entry]


def test_write_todos_and_blank_text_add_nothing_and_do_not_split_a_group():
    todos = tool_call("w1", "write_todos", {"todos": []}, result="ok")
    b = _blocks([user("u1"), asst("a1", read("c1"), todos, text("  "), read("c2"))], in_flight=True)[0]
    assert _kinds(b["items"]) == ["group"] and len(b["items"][0]["steps"]) == 2


def test_every_group_carries_its_line_and_thinking_adds_no_clause():
    b = _blocks([user("u1"), asst("a1", thinking(), read("c1"), bash("c2"))], in_flight=True)[0]
    line = b["items"][0]["line"]
    assert (line["text"], line["icon"], line["running"]) == ("Read 1 file, ran 1 command", "light-bulb", None)


@pytest.mark.parametrize(
    "segs,expected",
    [
        ([timed_thought("r1", 1200), timed_thought("r2", 3600)], "Thought for 5s"),
        ([timed_thought("r1", 1200), thinking()], "Thought"),
    ],
)
def test_a_group_of_only_thinking_reads_thought(segs, expected):
    assert _group_line(segs)["text"] == expected


def test_clauses_split_by_platform_and_mcp_server_and_only_the_first_is_capitalised():
    gitlab = tool_call("g1", "gitlab", {"subcommand": "mr view"}, result="ok")
    segs = [gitlab, {**gitlab, "id": "g2"}, tool_call("m1", "sentry_find", {}, result="ok")]
    segs.append(tool_call("m2", "linear_list", {}, result="ok"))
    line = _group_line(segs, servers=["sentry", "linear"])
    assert line["text"] == "Called GitLab 2 times, used sentry, used linear"


def test_created_files_get_their_own_clause():
    write = tool_call("c1", "write_file", {"file_path": "/workspace/repo/n.py", "content": "a\n"}, result="ok")
    assert _group_line([write, edit("c2", "a\n", "b\n")])["text"] == "Created 1 file, edited 1 file"


def test_group_line_names_the_last_running_step():
    b = _blocks([user("u1"), asst("a1", read("c1"), bash("c2", status="running"), streaming=True)])[0]
    assert b["items"][0]["line"]["running"] == "Running pytest"


def test_a_second_build_reuses_rows_of_unchanged_segments():
    expr = """(() => {
      let calls = 0;
      const toolRow = window.toolRow;
      window.toolRow = (seg, ctx) => { calls++; return toolRow(seg, ctx); };
      const counts = [];
      const build = (names) => {
        const before = calls;
        const mcpRows = names.map((name) => ({ name }));
        buildRunBlocks(vars.turns, {inFlight: false, latestArtifactsById: new Map(), mcpRows});
        counts.push(calls - before);
      };
      build(["sentry"]);
      build(["sentry"]);
      vars.turns[1].segments[1].result = "error: gone";
      build(["sentry"]);
      build(["sentry", "linear"]);
      return counts;
    })()"""
    turns = [user("u1"), asst("a1", read("c1"), read("c2"), bash("c3"))]
    assert run_transcript(expr, {"turns": turns}) == [3, 0, 1, 3]


def test_a_second_build_reuses_artifacts_and_group_lines_of_unchanged_segments():
    expr = """(() => {
      let calls = 0;
      const artifactItem = window.artifactItem;
      window.artifactItem = (seg, latest) => { calls++; return artifactItem(seg, latest); };
      const latest = new Map();
      const build = () => buildRunBlocks(vars.turns, {inFlight: true, latestArtifactsById: latest, mcpRows: []});
      const first = build();
      const second = build();
      return [calls, first[0].items[0].line === second[0].items[0].line];
    })()"""
    turns = [user("u1"), asst("a1", read("c1"), read("c2"), publish("p1"), streaming=True)]
    assert run_transcript(expr, {"turns": turns}) == [1, True]


def test_a_text_segment_found_not_blank_is_not_read_again():
    expr = """(() => {
      let reads = 0;
      vars.turns[1].segments.push({ type: "text", get content() { reads++; return "Found it."; } });
      for (let i = 0; i < 3; i++) buildRunBlocks(vars.turns, {inFlight: true, mcpRows: []});
      return reads;
    })()"""
    assert run_transcript(expr, {"turns": [user("u1"), asst("a1", read("c1"))]}) == 1


def test_step_keys_survive_in_place_replay_mutation():
    expr = f"""(() => {{
      const keys = {KEYS};
      const before = keys({BUILD});
      const turn = vars.turns[1];
      turn.segments = turn.segments.filter((s) => s.type !== "thinking");
      turn.segments[0].result = "1  x";
      turn.segments[0].status = "done";
      return [before, keys({BUILD})];
    }})()"""
    live = {"type": "thinking", "id": "r1", "content": "", "status": "running", "startedAt": 1, "endedAt": None}
    turns = [user("u1"), asst("a1", live, read("c1", result=None, status="running"), read("c2"), streaming=True)]
    before, after = run_transcript(expr, {"turns": turns, "inFlight": True, "servers": []})
    assert {"s-c1", "s-c2"} <= set(before) & set(after)


def test_duplicate_segment_ids_still_get_unique_keys():
    keys = _keys([user("u1"), asst("a1", timed_thought("r1", 10), timed_thought("r1", 10), read("c1"))])
    assert len(keys) == len(set(keys))


def test_run_status_attaches_to_the_preceding_block_and_does_not_end_it():
    blocks = _blocks([user("u1"), asst("a1", read("c1")), status_turn("aborted"), user("u2"), asst("a2", text("ok"))])
    assert [len(b["statuses"]) for b in blocks] == [1, 0]
    assert blocks[0]["endedAt"] == ENDED and blocks[0]["lastTurn"]["id"] == "a1"


def test_a_marker_carrying_its_own_run_id_is_its_own_block():
    marker = {**status_turn("failed"), "run_id": "run-9"}
    blocks = _blocks([user("u1"), asst("a1", text("ok")), marker])
    assert [len(b["statuses"]) for b in blocks] == [0, 1]
    assert blocks[1]["runId"] == "run-9"


def test_a_turn_carrying_a_new_run_id_starts_a_block_without_an_anchor():
    artifacts = {**asst("x1", publish("p1")), "run_id": "run-9"}
    blocks = _blocks([user("u1"), asst("a1", text("ok")), artifacts])
    assert [b["anchor"] and b["anchor"]["id"] for b in blocks] == ["u1", None]
    assert blocks[1]["runId"] == "run-9" and blocks[0]["key"] == "u1"


def test_a_stamped_turn_after_an_anchor_without_a_run_id_stays_in_its_block():
    anchor = {k: v for k, v in user("u1").items() if k != "run_id"}
    blocks = _blocks([anchor, {**asst("a1", read("c1")), "run_id": "run-u1"}])
    assert [(b["key"], b["anchor"] and b["anchor"]["id"], b["stats"]["steps"]) for b in blocks] == [("u1", "u1", 1)]


def test_while_in_flight_the_last_block_is_live_even_when_it_is_a_pending_prompt():
    blocks = _blocks([user("u1"), asst("a1", read("c1")), user("u2", pending=True)], in_flight=True)
    assert [b["live"] for b in blocks] == [False, True] and blocks[0]["folded"] is True


def test_block_key_is_the_anchor_id_with_or_without_its_run_id():
    turns = [user("u1"), asst("a1", read("c1"))]
    streamed = [{k: v for k, v in turns[0].items() if k != "run_id"}, turns[1]]
    assert _blocks(turns)[0]["key"] == _blocks(streamed)[0]["key"] == "u1"


def test_question_item_carries_its_turn_index():
    b = _blocks([user("u1"), asst("a1", read("c1")), asst("a2", question("q1"))])[0]
    assert b["endingTail"][0]["turnIndex"] == 2


def test_a_block_carries_its_last_turn():
    turns = [user("u1"), asst("a1", read("c1")), asst("a2", text("ok")), user("u2"), asst("a3", text("hi"))]
    assert [b["lastTurn"]["id"] for b in _blocks(turns)] == ["a2", "a3"]


def test_the_copy_turn_is_the_last_with_text_even_when_a_tool_only_turn_follows():
    b = _blocks([user("u1"), asst("a1", text("Final answer"), bash("c1")), asst("a2", read("c2"))])[0]
    assert (b["lastTurn"]["id"], b["copyTurn"]["id"]) == ("a2", "a1")


def test_a_streaming_run_offers_no_copy_turn():
    b = _blocks([user("u1"), asst("a1", text("Partial")), asst("a2", read("c1"), streaming=True)])[0]
    assert b["copyTurn"] is None


def test_a_question_the_agent_could_not_ask_carries_a_failed_row():
    item = _blocks([user("u1"), asst("a1", refused_question("q1"))])[0]["items"][0]
    assert item["kind"] == "question" and item["row"]["failed"] is True


REBUILD = """(() => {
  const build = (previous) =>
    buildRunBlocks(vars.turns, {latestArtifactsById: new Map(), mcpRows: [], previous});
  const first = build();
  const again = build(first);
  vars.turns[3].segments[0].content += " more";
  const streamed = build(again);
  Object.assign(vars.turns[3].segments[1], {result: "1  x", status: "done"});
  const landed = build(streamed);
  return {
    again: again === first,
    streamed: streamed === first,
    finished: landed[0] === first[0],
    finishedItems: landed[0].items.every((item, i) => item === first[0].items[i]),
    liveText: landed[1].items[0] === first[1].items[0],
    liveStep: landed[1].items[1] === first[1].items[1],
  };
})()"""


def test_a_changed_group_keeps_the_objects_of_its_unchanged_steps():
    expr = """(() => {
      const build = (previous) => buildRunBlocks(vars.turns, {inFlight: true, mcpRows: [], previous});
      const first = build();
      vars.turns[1].segments[1].args = '{"file_path": "/workspace/repo/b.py"}';
      const next = build(first);
      const [before, after] = [first[0].items[0], next[0].items[0]];
      return [after === before, after.steps.map((step, i) => step === before.steps[i])];
    })()"""
    turns = [user("u1"), asst("a1", read("c1"), read("c2", result=None, status="running"), streaming=True)]
    assert run_transcript(expr, {"turns": turns}) == [False, [True, False]]


def test_a_rebuild_keeps_every_block_and_item_that_did_not_change():
    turns = [
        user("u1"),
        asst("a1", read("c1"), read("c2"), text("Done.")),
        user("u2"),
        asst("a2", text("Looking"), read("c3", result=None, status="running"), streaming=True),
    ]
    assert run_transcript(REBUILD, {"turns": turns}) == {
        "again": True,
        "streamed": True,
        "finished": True,
        "finishedItems": True,
        "liveText": True,
        "liveStep": False,
    }


def test_a_changed_mcp_server_icon_or_read_only_list_re_derives_its_rows():
    expr = """(() => {
      let calls = 0;
      const toolRow = window.toolRow;
      window.toolRow = (seg, ctx) => { calls++; return toolRow(seg, ctx); };
      const seen = [];
      const build = (icon, readOnly) => {
        const before = calls;
        const mcpRows = [{ name: "sentry", icon, read_only_tools: readOnly }];
        const opts = { inFlight: false, latestArtifactsById: new Map(), mcpRows };
        const blocks = buildRunBlocks(vars.turns, opts);
        seen.push([calls - before, blocks[0].items[0].row.iconSrc]);
      };
      build("", []);
      build("", []);
      build("https://x.test/a.png", []);
      build("https://x.test/a.png", ["search_issues"]);
      build("https://x.test/a.png", ["search_issues"]);
      return seen;
    })()"""
    turns = [user("u1"), asst("a1", tool_call("c1", "sentry_search_issues", {"query": "x"}, result="[]"))]
    assert run_transcript(expr, {"turns": turns}) == [
        [1, ""],
        [0, ""],
        [1, "https://x.test/a.png"],
        [1, "https://x.test/a.png"],
        [0, "https://x.test/a.png"],
    ]
