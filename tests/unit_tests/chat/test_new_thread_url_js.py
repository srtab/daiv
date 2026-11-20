from django.urls import resolve

import pytest

from tests.unit_tests.chat.chat_stream_driver import run_chat_stream
from tests.unit_tests.jsdriver import requires_node

pytestmark = requires_node

BODY = """
const chat = registry.chat({ endpoint: "/api/chat" });
Object.assign(chat, { canSend: () => true, scrollToBottom: () => {}, $nextTick: () => {} });
let path = null;
sandbox.history.replaceState = (_state, _title, url) => {
  path = url;
  throw new Error("stop before the request");
};
await chat.submit("hi").catch(() => {});
process.stdout.write(JSON.stringify({ path, threadId: chat.thread.thread_id }));
"""


def test_a_new_thread_moves_the_address_bar_to_its_session_page():
    out = run_chat_stream(BODY, {}, extra_globals="history: {},")

    match = resolve(out["path"])
    assert match.url_name == "session_detail"
    assert match.kwargs == {"thread_id": out["threadId"]}


HAND_OVER_BODY = """
let reloads = 0;
let streamed = 0;
sandbox.window.location.reload = () => { reloads += 1; };
sandbox.history.replaceState = () => {};
const chat = registry.chat({ endpoint: "/api/chat" });
Object.assign(chat, {
  canSend: () => true,
  scrollToBottom: () => {},
  autosize: () => {},
  $nextTick: () => {},
  _postRun: async () => ({ ok: true, json: async () => payload.reply }),
  _streamRun: async () => { streamed += 1; return "finished"; },
});
await chat.submit("hi");
process.stdout.write(JSON.stringify({ reloads, streamed }));
"""


@pytest.mark.parametrize(
    "reply",
    [{"run_id": "r1"}, {"run_id": "r1", "queued": {"event_id": "e1", "run_id": "next-run", "position": "next"}}],
    ids=["started", "queued"],
)
def test_a_new_thread_hands_its_accepted_message_to_the_session_page(reply):
    out = run_chat_stream(HAND_OVER_BODY, {"reply": reply}, extra_globals="history: {},")

    assert out == {"reloads": 1, "streamed": 0}
