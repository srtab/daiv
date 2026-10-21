from django.urls import resolve

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
