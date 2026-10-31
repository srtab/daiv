import uuid

from django.utils import timezone

import pytest
from sessions.inbox.ownership import aclaim_ownership, aownership_for_branch, aowning_session
from sessions.models import EventKind, InboxEvent, MergeRequestOwnership, Run, RunStatus, Session, SessionOrigin

from accounts.models import User

pytestmark = pytest.mark.django_db(transaction=True)

REPO = "group/repo"
MR = {"merge_request_id": 9, "source_branch": "chore/install-gettext", "title": "Install gettext"}


async def _user(username: str) -> User:
    return await User.objects.acreate(username=username, email=f"{username}@example.com")


async def _publishing_run(user: User | None, trigger: str = SessionOrigin.CHAT) -> Run:
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id=REPO, ref=MR["source_branch"], user=user
    )
    return await Run.objects.acreate(
        session=session, trigger_type=trigger, repo_id=REPO, status=RunStatus.RUNNING, user=user
    )


async def _claim(run: Run, sha: str = "e96f1f77") -> MergeRequestOwnership:
    return await aclaim_ownership(run_id=run.pk, merge_request=MR, pushed_sha=sha)


async def _events(session_id: str) -> list[InboxEvent]:
    return [event async for event in InboxEvent.objects.filter(session_id=session_id)]


class TestClaimRules:
    async def test_the_first_publish_creates_the_ownership(self):
        alice = await _user("alice")
        run = await _publishing_run(alice)

        ownership = await _claim(run)

        assert (ownership.repo_id, ownership.merge_request_iid) == (REPO, 9)
        assert (ownership.session_id, ownership.owner_id) == (run.session_id, alice.pk)
        assert ownership.source_branch == "chore/install-gettext"
        assert ownership.last_daiv_sha == "e96f1f77"

    async def test_a_publish_from_another_session_of_the_same_person_moves_it(self):
        alice = await _user("alice")
        first, second = await _publishing_run(alice), await _publishing_run(alice)
        await _claim(first)

        ownership = await _claim(second, sha="0a1b2c3d")

        assert ownership.session_id == second.session_id
        assert ownership.last_daiv_sha == "0a1b2c3d"
        assert await _events(first.session_id) == []

    async def test_two_ownerless_sessions_count_as_the_same_person(self):
        first, second = await _publishing_run(None), await _publishing_run(None)
        await _claim(first)

        assert (await _claim(second)).session_id == second.session_id

    async def test_a_session_with_a_user_takes_it_from_an_ownerless_one_and_tells_it(self):
        alice = await _user("alice")
        ownerless, owned = await _publishing_run(None), await _publishing_run(alice)
        await _claim(ownerless)

        ownership = await _claim(owned)

        assert (ownership.session_id, ownership.owner_id) == (owned.session_id, alice.pk)
        [notice] = await _events(ownerless.session_id)
        assert (notice.kind, notice.wake) == (EventKind.NOTICE, False)
        assert "a session of alice" in notice.payload["text"]

    async def test_another_persons_publish_leaves_it_and_tells_the_owner(self):
        alice, bob = await _user("alice"), await _user("bob")
        owner, other = await _publishing_run(alice), await _publishing_run(bob)
        await _claim(owner)

        ownership = await _claim(other, sha="0a1b2c3d")

        assert (ownership.session_id, ownership.owner_id) == (owner.session_id, alice.pk)
        assert ownership.last_daiv_sha == "0a1b2c3d"
        [event] = await _events(owner.session_id)
        assert (event.kind, event.wake, event.ref) == (EventKind.FOREIGN_PUBLISH, False, "chore/install-gettext")
        assert event.payload == {"thread_id": other.session_id, "owner_username": "bob", "sha": "0a1b2c3d"}
        assert await _events(other.session_id) == []

    async def test_a_deleted_owners_session_counts_as_no_row_for_the_same_person(self):
        alice = await _user("alice")
        gone, current = await _publishing_run(alice), await _publishing_run(alice)
        await _claim(gone)
        await Session.objects.filter(pk=gone.session_id).adelete()

        assert (await _claim(current)).session_id == current.session_id

    async def test_a_deleted_owners_session_still_keeps_it_from_another_person(self):
        alice, bob = await _user("alice"), await _user("bob")
        gone, other = await _publishing_run(alice), await _publishing_run(bob)
        await _claim(gone)
        await Session.objects.filter(pk=gone.session_id).adelete()

        ownership = await _claim(other)

        assert (ownership.session_id, ownership.owner_id) == (None, alice.pk)

    async def test_an_unknown_merge_request_claims_nothing(self):
        run = await _publishing_run(None)

        assert await aclaim_ownership(run_id=run.pk, merge_request=None, pushed_sha="e96f1f77") is None
        assert not await MergeRequestOwnership.objects.aexists()


class TestAttempts:
    @pytest.mark.parametrize(
        "trigger",
        [
            SessionOrigin.CHAT, SessionOrigin.MR_WEBHOOK, SessionOrigin.API_JOB, SessionOrigin.MCP_JOB,
            SessionOrigin.UI_JOB, SessionOrigin.ISSUE_WEBHOOK, SessionOrigin.SCHEDULE,
        ],
    )  # fmt: skip
    async def test_a_publish_a_person_started_gives_the_attempts_back(self, trigger):
        alice = await _user("alice")
        await _claim(await _publishing_run(alice))
        await MergeRequestOwnership.objects.aupdate(attempts=3)

        assert (await _claim(await _publishing_run(alice, trigger))).attempts == 0

    @pytest.mark.parametrize(
        "trigger", [SessionOrigin.PIPELINE_WEBHOOK, SessionOrigin.DELEGATION_RESUME, SessionOrigin.DELEGATED_JOB]
    )
    async def test_a_publish_daiv_started_keeps_the_attempts_spent(self, trigger):
        alice = await _user("alice")
        await _claim(await _publishing_run(alice))
        await MergeRequestOwnership.objects.aupdate(attempts=2)

        assert (await _claim(await _publishing_run(alice, trigger))).attempts == 2


class TestOwningSession:
    async def test_it_is_the_owning_session(self):
        run = await _publishing_run(None)
        await _claim(run)

        assert (await aowning_session(REPO, 9)).pk == run.session_id

    async def test_an_unowned_or_closed_merge_request_has_none(self):
        await _claim(await _publishing_run(None))
        await MergeRequestOwnership.objects.aupdate(closed_at=timezone.now())

        assert await aowning_session(REPO, 9) is None
        assert await aowning_session(REPO, 10) is None

    async def test_a_deleted_owning_session_is_replaced_by_one_for_the_same_person(self):
        alice = await _user("alice")
        run = await _publishing_run(alice)
        await _claim(run)
        await Session.objects.filter(pk=run.session_id).adelete()

        session = await aowning_session(REPO, 9, title="Install gettext")

        assert session.pk != run.session_id
        assert (session.user_id, session.origin, session.ref) == (
            alice.pk,
            SessionOrigin.PIPELINE_WEBHOOK,
            MR["source_branch"],
        )
        assert session.title == "Install gettext"
        assert (await MergeRequestOwnership.objects.aget()).session_id == session.pk


class TestOwnershipForBranch:
    async def test_it_finds_the_open_ownership_of_a_branch(self):
        await _claim(await _publishing_run(None))

        assert (await aownership_for_branch(REPO, MR["source_branch"])).merge_request_iid == 9
        assert await aownership_for_branch(REPO, "other") is None

    async def test_a_closed_one_is_not_found(self):
        await _claim(await _publishing_run(None))
        await MergeRequestOwnership.objects.aupdate(closed_at=timezone.now())

        assert await aownership_for_branch(REPO, MR["source_branch"]) is None
