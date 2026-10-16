"""``derive_context_usage`` — the reload half of the context meter."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest import mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sessions.hydration import ahydrate_thread, derive_context_usage

from automation.agent.events import context_usage_payload
from tests.unit_tests.sessions.conftest import acreate_archive, amake_job_session, fake_checkpointer, make_merge_request

USAGE = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "input_token_details": {"cache_read": 60}}


def _ai(usage=None, model=None, content="ok") -> AIMessage:
    kwargs = {}
    if usage is not None:
        kwargs["usage_metadata"] = usage
    if model is not None:
        kwargs["response_metadata"] = {"model_name": model}
    return AIMessage(content=content, **kwargs)


def test_the_seed_comes_from_the_last_ai_message_carrying_usage():
    """A middleware-minted reply (no usage) and the trailing user turn are skipped — the
    reading is the last model call, same as the live event."""
    older = _ai(usage={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}, model="anthropic/claude-sonnet-4.6")
    latest = _ai(usage=USAGE, model="anthropic/claude-sonnet-4.6")

    seed = derive_context_usage([older, latest, _ai(), HumanMessage(content="hi")])

    assert seed["used_tokens"] == 110
    assert seed["cached_tokens"] == 60
    assert seed["window_tokens"] == 1_000_000


def test_no_usage_anywhere_yields_no_seed():
    assert derive_context_usage([HumanMessage(content="hi"), _ai()]) is None


def test_an_unknown_model_seeds_the_count_without_a_window():
    seed = derive_context_usage([_ai(usage=USAGE, model="model-nobody-knows")])

    assert seed["used_tokens"] == 110
    assert seed["window_tokens"] is None


def test_a_doubled_streamed_name_still_resolves_its_window():
    """The reload path for a session recorded before the source-level dedupe. A doubled name
    matches no window, and the meter no longer narrates that state, so the failure would be a
    silent bare count where the ring belongs.
    """
    seed = derive_context_usage([_ai(usage=USAGE, model="anthropic/claude-sonnet-4.6" * 2)])

    assert seed["model"] == "anthropic/claude-sonnet-4.6"
    assert seed["window_tokens"] == 1_000_000


def test_seed_and_middleware_payload_share_the_wire_key_set():
    """Both producers go through the one builder, asserted on key sets."""
    seed = derive_context_usage([_ai(usage=USAGE, model="anthropic/claude-sonnet-4.6")])
    built = context_usage_payload(model="m", usage={"input_tokens": 1, "output_tokens": 1}, window=None)

    assert set(seed) == set(built)


@pytest.mark.django_db(transaction=True)
async def test_a_seed_failure_costs_the_meter_not_the_thread():
    """A checkpoint shape the seed walk can't parse must not take down the page render or
    the transcript poller — the transcript is a usable partial result."""

    @asynccontextmanager
    async def _fake_checkpointer():
        yield SimpleNamespace()

    messages = [_ai(usage=USAGE, model="anthropic/claude-sonnet-4.6")]
    with (
        mock.patch("sessions.hydration.open_checkpointer", _fake_checkpointer),
        mock.patch("sessions.hydration.aread_thread_values", mock.AsyncMock(return_value={"messages": messages})),
        mock.patch("sessions.hydration.derive_context_usage", side_effect=ValueError("unmodeled shape")),
    ):
        hydrated = await ahydrate_thread("thread-1")

    assert hydrated.messages == messages
    assert hydrated.expired is False
    assert hydrated.context_usage is None


@asynccontextmanager
async def _live(values: dict | None):
    yield fake_checkpointer(values)


async def _hydrate(thread_id: str, live: dict | None):
    with mock.patch("sessions.hydration.open_checkpointer", lambda: _live(live)):
        return await ahydrate_thread(thread_id)


def _human(id_: str) -> HumanMessage:
    return HumanMessage(content=f"turn {id_}", id=id_)


def _reply(id_: str) -> AIMessage:
    return AIMessage(content=f"reply {id_}", id=id_)


def _ids(messages: list) -> list[str]:
    return [m.id for m in messages]


@pytest.mark.django_db(transaction=True)
class TestArchivedHydration:
    async def test_an_expired_thread_is_read_from_its_archive(self):
        thread_id = await amake_job_session()
        await acreate_archive(thread_id, 0, [_human("h1"), _reply("a1")], diff_stats={"added": 2})

        hydrated = await _hydrate(thread_id, None)

        assert hydrated.expired is False
        assert _ids(hydrated.messages) == ["h1", "a1"]
        assert hydrated.diff_stats == {"added": 2}

    async def test_an_expired_thread_shows_every_context_with_the_payloads_of_the_latest(self):
        thread_id = await amake_job_session()
        await acreate_archive(thread_id, 0, [_human("h1"), _reply("a1")], closed=True, diff_stats={"added": 1})
        await acreate_archive(
            thread_id, 1, [_human("h2"), _reply("a2")], diff_stats={"added": 2}, merge_request=make_merge_request()
        )

        hydrated = await _hydrate(thread_id, None)

        assert _ids(hydrated.messages) == ["h1", "a1", "h2", "a2"]
        assert hydrated.diff_stats == {"added": 2}
        assert hydrated.merge_request_payload["id"] == 7

    @pytest.mark.parametrize(
        ("contexts", "live", "expected"),
        [
            pytest.param(
                [([_human("h1"), _reply("a1")], True), ([_reply("r1")], False)],
                [_reply("r1"), _human("h2"), _reply("a2")],
                ["h1", "a1", "r1", "h2", "a2"],
                id="contexts-before-a-clear-precede-the-live-one",
            ),
            pytest.param(
                [([_human("h1"), _reply("a1")], True)],
                [_reply("r1")],
                ["h1", "a1", "r1"],
                id="a-live-context-not-archived-yet-follows-every-archived-one",
            ),
            pytest.param(
                [([_human("x"), _reply("a1")], True)],
                [_human("x"), _reply("a2")],
                ["x", "a1", "x", "a2"],
                id="a-cleared-context-is-kept-when-the-next-one-starts-with-its-first-message",
            ),
            pytest.param(
                [([_human("h1"), _reply("a1"), _human("h2")], False)],
                [_human("h2"), _reply("a2")],
                ["h1", "a1", "h2", "a2"],
                id="a-live-thread-that-lost-its-oldest-messages-is-not-duplicated",
            ),
            pytest.param(
                [([_human("p"), _reply("a1")], True), ([_reply("r1")], False)],
                [_reply("r1"), _human("p"), _reply("a2")],
                ["p", "a1", "r1", "p", "a2"],
                id="a-turn-after-a-clear-keeps-an-id-an-earlier-context-used",
            ),
        ],
    )
    async def test_the_live_thread_follows_the_archived_contexts(self, contexts, live, expected):
        thread_id = await amake_job_session()
        for epoch, (messages, closed) in enumerate(contexts):
            await acreate_archive(thread_id, epoch, messages, closed=closed)

        hydrated = await _hydrate(thread_id, {"messages": live})

        assert _ids(hydrated.messages) == expected

    async def test_the_meter_reads_the_current_context_only(self):
        thread_id = await amake_job_session()
        await acreate_archive(
            thread_id, 0, [_human("h1"), _ai(usage=USAGE, model="anthropic/claude-sonnet-4.6")], closed=True
        )

        hydrated = await _hydrate(thread_id, {"messages": [_reply("r1")]})

        assert hydrated.context_usage is None

    async def test_an_earlier_context_that_fails_to_load_is_left_out(self, caplog):
        thread_id = await amake_job_session()
        await acreate_archive(thread_id, 0, [_human("u0")], closed=True, unreadable=True)
        await acreate_archive(thread_id, 1, [_human("h2")])

        with caplog.at_level("ERROR", logger="daiv.sessions"):
            hydrated = await _hydrate(thread_id, None)

        assert _ids(hydrated.messages) == ["h2"]
        assert f"Failed to load archived context 0 of thread {thread_id}" in caplog.text

    async def test_a_latest_context_that_fails_to_load_gives_way_to_the_one_before(self):
        thread_id = await amake_job_session()
        await acreate_archive(thread_id, 0, [_human("h1")], closed=True)
        await acreate_archive(thread_id, 1, [_human("u1")], unreadable=True)

        hydrated = await _hydrate(thread_id, None)

        assert (hydrated.expired, _ids(hydrated.messages)) == (False, ["h1"])

    async def test_a_live_thread_is_shown_alone_when_its_open_context_fails_to_load(self, caplog):
        thread_id = await amake_job_session()
        await acreate_archive(thread_id, 0, [_human("u0")], unreadable=True)

        with caplog.at_level("ERROR", logger="daiv.sessions"):
            hydrated = await _hydrate(thread_id, {"messages": [_human("h2"), _reply("a2")]})

        assert _ids(hydrated.messages) == ["h2", "a2"]
        assert f"Failed to load archived context 0 of thread {thread_id}" in caplog.text

    async def test_a_merge_request_that_no_longer_revives_shows_none(self):
        """The view then falls back to looking the merge request up on the platform."""
        thread_id = await amake_job_session()
        await acreate_archive(thread_id, 0, [_human("h1")], merge_request={"repo_id": "group/repo"})

        hydrated = await _hydrate(thread_id, None)

        assert hydrated.merge_request_payload is None

    async def test_a_thread_with_neither_checkpoint_nor_archive_is_expired(self):
        thread_id = await amake_job_session()

        hydrated = await _hydrate(thread_id, None)

        assert (hydrated.expired, hydrated.messages) == (True, [])
