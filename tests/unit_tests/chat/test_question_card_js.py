"""Behavioral tests for the ask-the-user card in ``chat-stream.js``, driven under node."""

from __future__ import annotations

import json

from automation.agent.questions import QUESTION_DELIVERED
from tests.unit_tests.chat.chat_stream_driver import CHAT_STREAM_JS, run_chat_stream
from tests.unit_tests.jsdriver import requires_node

pytestmark = requires_node

QUESTIONS = {
    "questions": [
        {
            "header": "Database",
            "question": "Which engine?",
            "options": [{"label": "PostgreSQL", "description": "p"}, {"label": "SQLite", "description": "s"}],
            "multi_select": False,
        },
        {
            "header": "Targets",
            "question": "Which apps?",
            "options": [{"label": "API", "description": "a"}, {"label": "UI", "description": "u"}],
            "multi_select": True,
        },
    ]
}


def _card(result=QUESTION_DELIVERED, args=None):
    return {
        "type": "tool_call",
        "id": "ask-1",
        "name": "ask_user_question",
        "args": json.dumps(QUESTIONS) if args is None else args,
        "result": result,
        "status": "done",
    }


BODY = """
const { turns, script } = payload;
const chat = registry.chat({ endpoint: "/api/chat" });
chat.turns = turns;
chat.thread = { thread_id: "t1", repo_id: "r", ref: "main" };
chat.$nextTick = (cb) => cb();
chat.scrollToBottom = () => {};
const sent = [];
chat.submit = async (text) => { sent.push(text); };
const seg = chat.turns[1].segments[0];
const out = {};
for (const step of script) {
  if (step.op === "toggle") chat.toggleQuestionOption(seg, step.qi, step.label);
  if (step.op === "type") chat.setQuestionText(seg, step.qi, step.value);
  if (step.op === "streaming") chat.streaming = step.value;
  if (step.op === "setDraft") chat.draftMessage = step.value;
  if (step.op === "answer") await chat.answerQuestion(seg);
  if (step.op === "read") out[step.key] = {
    card: chat.isQuestionCard(seg),
    open: chat.isQuestionOpen(1),
    questions: chat.questionsOf(seg).length,
    answer: chat.composeAnswer(seg),
    canAnswer: chat.canAnswerQuestion(seg),
    selected: chat.isOptionSelected(seg, 0, "SQLite"),
    draft: chat.draftMessage,
  };
}
out.sent = sent;
process.stdout.write(JSON.stringify(out));
"""


def _drive(script, *, segment=None, extra_turns=()):
    turns = [
        {"id": "u-1", "role": "user", "segments": [{"type": "text", "content": "migrate"}]},
        {"id": "a-1", "role": "assistant", "segments": [segment or _card()]},
        *extra_turns,
    ]
    return run_chat_stream(BODY, {"turns": turns, "script": script})


def test_a_delivered_question_in_the_last_turn_is_an_open_card():
    out = _drive([{"op": "read", "key": "s"}])
    assert out["s"]["card"] is True
    assert out["s"]["open"] is True
    assert out["s"]["questions"] == 2


def test_a_failed_question_is_not_a_card():
    assert _drive([{"op": "read", "key": "s"}], segment=_card(result="Error: bad args"))["s"]["card"] is False


def test_half_streamed_args_parse_to_no_questions():
    assert _drive([{"op": "read", "key": "s"}], segment=_card(args='{"questions": [{"hea'))["s"]["questions"] == 0


def test_the_card_closes_while_a_run_is_in_flight_or_once_the_user_replied():
    assert _drive([{"op": "streaming", "value": True}, {"op": "read", "key": "s"}])["s"]["open"] is False
    reply = {"id": "u-2", "role": "user", "segments": [{"type": "text", "content": "x"}]}
    assert _drive([{"op": "read", "key": "s"}], extra_turns=[reply])["s"]["open"] is False


def test_answer_composes_one_line_per_question():
    out = _drive([
        {"op": "toggle", "qi": 0, "label": "PostgreSQL"},
        {"op": "toggle", "qi": 0, "label": "SQLite"},
        {"op": "toggle", "qi": 1, "label": "API"},
        {"op": "toggle", "qi": 1, "label": "UI"},
        {"op": "read", "key": "s"},
    ])
    assert out["s"]["selected"] is True
    assert out["s"]["answer"] == "**Database** — SQLite\n**Targets** — API, UI"


def test_typed_text_wins_over_a_selection():
    out = _drive([
        {"op": "toggle", "qi": 0, "label": "SQLite"},
        {"op": "type", "qi": 0, "value": "  MariaDB  "},
        {"op": "type", "qi": 1, "value": "all of them"},
        {"op": "read", "key": "s"},
    ])
    assert out["s"]["answer"] == "**Database** — MariaDB\n**Targets** — all of them"


def test_an_unanswered_question_blocks_the_answer():
    out = _drive([{"op": "toggle", "qi": 0, "label": "SQLite"}, {"op": "answer"}, {"op": "read", "key": "s"}])
    assert out["s"]["canAnswer"] is False
    assert out["sent"] == []


def test_answer_sends_through_the_composer():
    out = _drive([
        {"op": "toggle", "qi": 0, "label": "SQLite"},
        {"op": "toggle", "qi": 1, "label": "UI"},
        {"op": "answer"},
    ])
    assert out["sent"] == ["**Database** — SQLite\n**Targets** — UI"]


def test_answering_a_question_leaves_a_pre_existing_draft_untouched():
    out = _drive([
        {"op": "setDraft", "value": "unrelated draft"},
        {"op": "toggle", "qi": 0, "label": "SQLite"},
        {"op": "toggle", "qi": 1, "label": "UI"},
        {"op": "answer"},
        {"op": "read", "key": "s"},
    ])
    assert out["sent"] == ["**Database** — SQLite\n**Targets** — UI"]
    assert out["s"]["draft"] == "unrelated draft"


def test_the_js_knows_the_delivery_text_the_tool_returns():
    assert json.dumps(QUESTION_DELIVERED) in CHAT_STREAM_JS.read_text()
