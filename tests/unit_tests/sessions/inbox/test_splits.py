import pytest
from sessions.inbox.splits import RequestSplits
from sessions.models import EventKind, SessionOrigin

from tests.unit_tests.sessions.inbox.conftest import amake_event, amake_session, chat_payload

pytestmark = pytest.mark.django_db(transaction=True)


async def _chat(session, text: str):
    return await amake_event(session, EventKind.CHAT_MESSAGE, payload=chat_payload(text))


async def _leg_follow_up(session):
    return await amake_event(
        session, EventKind.LEG_FOLLOW_UP, payload={"prompt": "Make the failing test pass", "batch_id": "b"}
    )


class TestLegFollowUp:
    @pytest.mark.parametrize("position", [0, 1, 2], ids=["before", "between", "after"])
    async def test_it_is_a_split_of_its_own_among_a_persons_requests(self, position):
        session = await amake_session()
        asked = [await _chat(session, "Fix the bug"), await _chat(session, "Then add a test")]
        follow_up = await _leg_follow_up(session)
        asked.insert(position, follow_up)

        splits = RequestSplits(asked).splits

        assert [follow_up] in splits
        assert sum(len(split) for split in splits) == 3
        assert all(follow_up not in split for split in splits if len(split) > 1)

    async def test_it_runs_alone_but_takes_context(self):
        follow_up = await _leg_follow_up(await amake_session())

        assert RequestSplits.runs_alone(follow_up)
        assert RequestSplits.takes_context(follow_up)


class TestTakesContext:
    async def test_a_builtin_slash_command_takes_none(self):
        command = await _chat(await amake_session(), "/help")

        assert RequestSplits.runs_alone(command)
        assert not RequestSplits.takes_context(command)

    async def test_an_ordinary_request_takes_it(self):
        session = await amake_session()
        chat = await _chat(session, "Fix the bug")
        job = await amake_event(
            session, EventKind.FOLLOW_UP_JOB, payload={"prompt": "Bump it", "trigger_type": SessionOrigin.API_JOB}
        )

        assert not RequestSplits.runs_alone(chat)
        assert RequestSplits.takes_context(chat)
        assert RequestSplits.takes_context(job)


async def test_requests_between_two_commands_still_share_a_run():
    session = await amake_session()
    first, command, second, third = [
        await _chat(session, text) for text in ("Fix the bug", "/help", "Then add a test", "And a changelog entry")
    ]

    assert RequestSplits([first, command, second, third]).splits == [[first], [command], [second, third]]
