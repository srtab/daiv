"""The spend half of the usage sheet in ``chat-stream.js``, driven under node.

Payloads come from the real ``build_session_spend`` over real ``Run`` rows, never a
hand-written fixture: the getters read hand-typed key literals off the payload — the same
one-sided-rename exposure the meter suite closes — so a key renamed in ``sessions/spend.py``
alone must fail here.
"""

from __future__ import annotations

import pytest
from sessions.models import Run, RunStatus, SessionOrigin
from sessions.spend import build_session_spend

from tests.unit_tests.chat.chat_pages import create_session
from tests.unit_tests.chat.chat_stream_driver import run_chat_stream
from tests.unit_tests.jsdriver import requires_node

pytestmark = requires_node

_PRELUDE = """
const fetches = [];
"""

_FETCH_STUB = """
  fetch: (url) => {
    fetches.push(String(url));
    const next = payload.responses.shift();
    return Promise.resolve({ ok: next.ok, status: next.status || 200, json: async () => next.spend });
  },
"""

_BODY = """
const chat = registry.chat({ endpoint: "/api/chat", spendEndpoint: "/api/chat/spend" });
chat.scrollToBottom = () => {};
chat.thread = { thread_id: payload.threadId };
const turn = { id: "t", role: "assistant", segments: [], streaming: true };
chat.turns = [turn];
for (let i = payload.responses.length; i > 0; i--) {
  chat._finishTurn(turn, "finished");
  await new Promise((r) => setTimeout(r, 0)); // _refreshSpend is fire-and-forget; drain it
}
process.stdout.write(JSON.stringify({
  fetches,
  header: chat.spendHeader,
  models: chat.spendRows.map((row) => row.model),
  rowCosts: chat.spendRows.map((row) => chat.spendRowCost(row)),
  unpriced: chat.spendUnpricedLine,
  unrecorded: chat.spendUnrecordedLine,
}));
"""


def _run(session, **kwargs) -> Run:
    defaults = {
        "trigger_type": SessionOrigin.CHAT,
        "status": RunStatus.SUCCESSFUL,
        "repo_id": "group/project",
        "ref": "main",
    }
    defaults.update(kwargs)
    return Run.objects.create(session=session, **defaults)


def _panel(responses) -> dict:
    return run_chat_stream(
        _BODY, {"threadId": "t-1", "responses": responses}, prelude=_PRELUDE, extra_globals=_FETCH_STUB
    )


@pytest.mark.django_db
def test_the_aggregator_payload_renders_the_panel(member_user):
    session = create_session(member_user)
    _run(
        session,
        input_tokens=100,
        output_tokens=10,
        total_tokens=110,
        usage_by_model={"m1": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "cost_usd": "0.10"}},
    )
    _run(
        session,
        input_tokens=200,
        output_tokens=20,
        total_tokens=220,
        usage_by_model={
            "m1": {"input_tokens": 150, "output_tokens": 15, "total_tokens": 165, "cost_usd": "0.20"},
            "m2": {"input_tokens": 50, "output_tokens": 5, "total_tokens": 55, "cost_usd": "0.01"},
        },
    )

    out = _panel([{"ok": True, "spend": build_session_spend(session.runs.all())}])

    assert out["fetches"] == ["/api/chat/spend?thread_id=t-1"]
    assert out["header"] == "2 turns · 330 · $0.31"
    assert out["models"] == ["m1", "m2"]
    assert out["rowCosts"] == ["$0.30", "$0.01"]


@pytest.mark.django_db
def test_floors_render_the_plus_and_their_footnote_lines(member_user):
    session = create_session(member_user)
    _run(
        session,
        input_tokens=100,
        output_tokens=10,
        total_tokens=110,
        usage_by_model={
            "priced": {"input_tokens": 60, "output_tokens": 6, "total_tokens": 66, "cost_usd": "0.10"},
            "mystery": {"input_tokens": 40, "output_tokens": 4, "total_tokens": 44},
        },
    )
    _run(session, status=RunStatus.FAILED)  # finalize failed: nothing recorded

    out = _panel([{"ok": True, "spend": build_session_spend(session.runs.all())}])

    assert out["header"].endswith("$0.10+")
    assert out["unpriced"] == "No price known for mystery"
    assert out["unrecorded"] == "1 turns with unrecorded usage"
    mystery = out["models"].index("mystery")
    assert out["rowCosts"][mystery] == "—"


@pytest.mark.django_db
def test_a_rejected_refresh_leaves_the_previous_reading_alone(member_user):
    """A 404/500 mid-session must not blank a panel that was correct — stale beats blank."""
    session = create_session(member_user)
    _run(
        session,
        input_tokens=100,
        output_tokens=10,
        total_tokens=110,
        usage_by_model={"m1": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "cost_usd": "0.10"}},
    )

    out = _panel([{"ok": True, "spend": build_session_spend(session.runs.all())}, {"ok": False, "status": 404}])

    assert len(out["fetches"]) == 2
    assert out["models"] == ["m1"]
    assert out["header"] == "1 turns · 110 · $0.10"


@pytest.mark.django_db
def test_an_empty_session_payload_renders_no_panel(member_user):
    """``normalizeSpend`` nulls a zero-turn payload, so a racing early refresh cannot
    paint "0 turns · 0"."""
    out = _panel([{"ok": True, "spend": build_session_spend(create_session(member_user).runs.all())}])

    assert out["header"] == ""
    assert out["models"] == []


def test_run_finished_alone_never_refreshes():
    """The refresh rides the stream's end (``_finishTurn``), strictly after
    ``finalize_chat_run`` persisted — a RUN_FINISHED refresh would race that write."""
    body = """
const chat = registry.chat({ endpoint: "/api/chat", spendEndpoint: "/api/chat/spend" });
chat.scrollToBottom = () => {};
chat.thread = { thread_id: "t-1" };
const turn = { id: "t", role: "assistant", segments: [], streaming: true };
chat.turns = [turn];
chat.dispatch({ type: "RUN_FINISHED" }, turn, 0);
await new Promise((r) => setTimeout(r, 0));
process.stdout.write(JSON.stringify({ fetches }));
"""
    out = run_chat_stream(body, {"responses": []}, prelude=_PRELUDE, extra_globals=_FETCH_STUB)

    assert out["fetches"] == []
