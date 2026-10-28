"""Which live failure marker ``chat-stream.js`` records for the session page's Retry, driven under node."""

from __future__ import annotations

import pytest

from tests.unit_tests.chat.chat_stream_driver import run_chat_stream
from tests.unit_tests.jsdriver import requires_node

pytestmark = requires_node

DISPATCH = """
const chat = registry.chat({ endpoint: "/api/chat" });
Object.assign(chat, { scrollToBottom: () => {}, _activeRun: { threadId: "t1", runId: "r1" } });
const turn = { id: "a-1", role: "assistant", segments: [] };
chat.turns = [turn];
chat.dispatch({ type: "RUN_ERROR", code: payload.code, message: "boom" }, turn, 0);
process.stdout.write(JSON.stringify({ marker: chat.runErrorMarkerId }));
"""

SUBMIT = """
const chat = registry.chat({ endpoint: "/api/chat" });
Object.assign(chat, { canSend: () => true, scrollToBottom: () => {}, $nextTick: () => {}, _refreshSpend: () => {} });
chat.thread = { thread_id: "t1", repo_id: "r", ref: "main" };
chat.runErrorMarkerId = "run-status-earlier";
await chat.submit("hi");
const marker = chat.turns.find((t) => t.role === "run_status");
process.stdout.write(JSON.stringify({ marker: chat.runErrorMarkerId, status: marker?.status }));
"""


def test_a_run_the_server_failed_records_its_marker():
    assert run_chat_stream(DISPATCH, {"code": "run_failed"}) == {"marker": "run-status-r1"}


@pytest.mark.parametrize("code", ["run_cancelled", "run_interrupted"])
def test_a_stopped_run_records_no_marker(code):
    assert run_chat_stream(DISPATCH, {"code": code}) == {"marker": ""}


def test_a_send_forgets_the_last_marker_and_a_failed_request_records_none():
    out = run_chat_stream(
        SUBMIT, {}, extra_globals='fetch: () => Promise.reject(new Error("down")), history: { replaceState() {} },'
    )

    assert out == {"marker": "", "status": "failed"}
