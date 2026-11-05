import pytest
from sessions.inbox.requester import Requester
from sessions.models import EventKind, Run, RunStatus, SessionOrigin

from tests.unit_tests.sessions.inbox.conftest import amake_event, amake_session

pytestmark = pytest.mark.django_db(transaction=True)


class TestOfEvent:
    async def test_an_authored_event_is_asked_by_its_author(self, admin_user):
        session = await amake_session()
        event = await amake_event(session, EventKind.CHAT_MESSAGE, author=admin_user)

        assert Requester.of_event(event, session) == Requester(admin_user.pk)

    async def test_a_person_without_a_linked_user_is_asked_by_their_platform_account(self):
        session = await amake_session()
        event = await amake_event(session, EventKind.MENTION, author_provider="gitlab", author_uid="42")

        assert Requester.of_event(event, session) == Requester(None, "gitlab", "42")

    async def test_daiv_waking_on_its_own_is_asked_by_the_session_owner(self, admin_user):
        session = await amake_session(user=admin_user)
        event = await amake_event(session, EventKind.CI_FAILED)

        assert Requester.of_event(event, session) == Requester(admin_user.pk)


class TestOfRun:
    async def test_it_reads_the_requester_off_the_run(self, admin_user):
        session = await amake_session()
        run = await Run.objects.acreate(
            session=session, repo_id="group/repo", status=RunStatus.QUEUED, trigger_type=SessionOrigin.CHAT,
            user=admin_user, external_provider="gitlab", external_uid="7",
        )  # fmt: skip

        assert Requester.of_run(run) == Requester(admin_user.pk, "gitlab", "7")


class TestRunFields:
    def test_the_username_names_an_unlinked_account(self):
        fields = Requester(None, "gitlab", "42").run_fields("sfr")

        assert fields == {
            "user_id": None,
            "external_provider": "gitlab",
            "external_uid": "42",
            "external_username": "sfr",
        }

    def test_a_linked_user_has_no_external_username(self):
        assert Requester(5).run_fields("sfr")["external_username"] == ""


class TestUser:
    async def test_an_unlinked_requester_has_no_user(self):
        session = await amake_session()

        assert Requester(None, "gitlab", "42").user(session, []) is None

    async def test_it_is_the_author_of_one_of_the_asked_events(self, admin_user, member_user):
        session = await amake_session(user=admin_user)
        event = await amake_event(session, EventKind.CHAT_MESSAGE, author=member_user)
        event.author = member_user

        assert Requester(member_user.pk).user(session, [event]) == member_user

    async def test_it_is_the_session_owner_when_no_asked_event_names_them(self, admin_user):
        session = await amake_session(user=admin_user)
        session.user = admin_user

        assert Requester(admin_user.pk).user(session, []) == admin_user
