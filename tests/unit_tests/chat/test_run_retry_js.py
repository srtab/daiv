"""Behavioral tests for retrying a failed run from its marker in ``chat-stream.js``, driven under node."""

from __future__ import annotations

import pytest

from tests.unit_tests.chat.chat_stream_driver import run_chat_stream
from tests.unit_tests.jsdriver import requires_node

pytestmark = requires_node

RETRY_MESSAGE = "The previous run failed. Please try again."

BODY = """
const { turns, flags } = payload;
const chat = registry.chat({ endpoint: "/api/chat" });
chat.turns = turns;
Object.assign(chat, flags);
const sent = [];
chat.submit = async (text) => { sent.push(text); };
const offered = turns.map((turn, ti) => chat.canRetry(turn, ti));
await chat.retryRun();
process.stdout.write(JSON.stringify({ offered, sent }));
"""


def _user(tid):
    return {"id": tid, "role": "user", "segments": [{"type": "text", "content": "go"}]}


def _marker(tid, status="failed"):
    return {"id": tid, "role": "run_status", "status": status, "message": "Run failed"}


def _drive(turns, **flags):
    return run_chat_stream(BODY, {"turns": turns, "flags": flags})


def test_the_failed_marker_ending_the_transcript_offers_retry_and_sends_the_retry_message():
    out = _drive([_user("u-1"), _marker("m-1")])

    assert out == {"offered": [False, True], "sent": [RETRY_MESSAGE]}


def test_an_aborted_run_offers_no_retry():
    assert _drive([_user("u-1"), _marker("m-1", status="aborted")])["offered"] == [False, False]


@pytest.mark.parametrize("later", [_user("u-2"), _marker("m-2")])
def test_only_the_newest_run_offers_retry(later):
    offered = _drive([_user("u-1"), _marker("m-1"), later])["offered"]

    assert offered[1] is False


@pytest.mark.parametrize("flags", [{"streaming": True}, {"resuming": True}, {"waitingSince": "2026-10-28T10:00:00Z"}])
def test_no_retry_while_a_run_is_in_flight_or_queued(flags):
    assert _drive([_user("u-1"), _marker("m-1")], **flags)["offered"] == [False, False]


@pytest.mark.parametrize("flag", ["streaming", "resuming"])
def test_retry_sends_nothing_while_a_run_is_in_flight(flag):
    assert _drive([_user("u-1"), _marker("m-1")], **{flag: True})["sent"] == []
