import uuid

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from sessions.inbox.ownership import Ownerships
from sessions.legs import Leg, LegMergeRequest
from sessions.models import (
    LINK_FAILED_PREFIX,
    MergeRequestOwnership,
    MergeRequestState,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
)
from sessions.queue import _rebase_on_waited_branch

from tests.unit_tests.sessions.conftest import make_coordinator, make_delegating_run, make_leg


def _follow_up_run(leg_run: Run, *, status: str = RunStatus.QUEUED, **fields) -> Run:
    fields = {"ref": leg_run.session.ref, "user": leg_run.user} | fields
    return Run.objects.create(
        session=leg_run.session,
        trigger_type=SessionOrigin.DELEGATED_JOB,
        batch_id=uuid.uuid4(),
        repo_id=leg_run.repo_id,
        status=status,
        **fields,
    )


@pytest.mark.django_db
class TestOf:
    def test_a_session_delegated_by_the_coordinator_is_its_leg(self, admin_user):
        coordinator = make_coordinator(admin_user)
        leg_run = make_leg(coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL)

        leg = Leg.of(leg_run.session_id, coordinator=coordinator.pk)

        assert leg is not None
        assert leg.session.pk == leg_run.session_id

    def test_another_coordinators_leg_is_not(self, admin_user):
        coordinator, other = make_coordinator(admin_user), make_coordinator(admin_user)
        leg_run = make_leg(other, uuid.uuid4(), status=RunStatus.SUCCESSFUL)

        assert Leg.of(leg_run.session_id, coordinator=coordinator.pk) is None

    def test_a_child_session_with_no_delegated_run_is_not(self, admin_user):
        coordinator = make_coordinator(admin_user)
        leg_run = make_leg(coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL)
        Run.objects.filter(pk=leg_run.pk).update(trigger_type=SessionOrigin.API_JOB)

        assert Leg.of(leg_run.session_id, coordinator=coordinator.pk) is None

    def test_a_session_that_does_not_exist_is_not(self, admin_user):
        coordinator = make_coordinator(admin_user)

        assert Leg.of(str(uuid.uuid4()), coordinator=coordinator.pk) is None


@pytest.mark.django_db
class TestBranch:
    def test_base_ref_is_the_first_delegated_runs_ref(self, admin_user):
        coordinator = make_coordinator(admin_user)
        leg_run = make_leg(coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL, ref="release", session_ref="release")
        _follow_up_run(leg_run, ref="daiv/leg-branch")

        assert Leg(leg_run.session).base_ref == "release"

    def test_base_ref_follows_a_waiting_legs_rebase(self, admin_user):
        coordinator = make_coordinator(admin_user, ref="feat/x")
        delegating = make_delegating_run(coordinator, admin_user)
        leg_run = make_leg(
            coordinator,
            uuid.uuid4(),
            status=RunStatus.QUEUED,
            repo_id="acme/coord",
            ref="feat/x",
            session_ref="feat/x",
            waits_for=delegating,
        )
        coordinator.ref = "daiv/coord-published"
        coordinator.save(update_fields=["ref"])

        _rebase_on_waited_branch(leg_run)

        leg_run.session.refresh_from_db()
        assert Leg(leg_run.session).base_ref == "daiv/coord-published"
        assert not Leg(leg_run.session).has_own_branch()

    def test_a_leg_on_the_branch_it_started_from_has_none_of_its_own(self, admin_user):
        coordinator = make_coordinator(admin_user)
        leg_run = make_leg(coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL, ref="release", session_ref="release")

        assert not Leg(leg_run.session).has_own_branch()

    def test_a_leg_that_moved_to_another_branch_has_one(self, admin_user):
        coordinator = make_coordinator(admin_user)
        leg_run = make_leg(
            coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL, ref="release", session_ref="daiv/leg-branch"
        )

        assert Leg(leg_run.session).has_own_branch()

    def test_from_the_default_branch_only_a_push_makes_the_branch_its_own(self, admin_user):
        coordinator = make_coordinator(admin_user)
        leg_run = make_leg(coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL, ref="", session_ref="main")

        assert not Leg(leg_run.session).has_own_branch()

        Run.objects.filter(pk=leg_run.pk).update(pushed_sha="abc123")
        leg_run.session.ref = "daiv/leg-branch"

        assert Leg(leg_run.session).has_own_branch()


@pytest.mark.django_db
class TestMergeRequest:
    def test_it_is_the_newest_its_runs_published_on_its_repository(self, admin_user):
        coordinator = make_coordinator(admin_user)
        leg_run = make_leg(
            coordinator,
            uuid.uuid4(),
            status=RunStatus.SUCCESSFUL,
            merge_request_iid=4,
            merge_request_web_url="https://gitlab.example/acme/api/-/merge_requests/4",
        )
        _follow_up_run(leg_run, status=RunStatus.SUCCESSFUL, merge_request_iid=7)
        _follow_up_run(
            leg_run,
            status=RunStatus.SUCCESSFUL,
            merge_request_iid=7,
            merge_request_web_url="https://gitlab.example/acme/api/-/merge_requests/7",
        )
        _follow_up_run(leg_run, status=RunStatus.SUCCESSFUL)

        assert Leg(leg_run.session).merge_request() == LegMergeRequest(
            "acme/api", 7, "https://gitlab.example/acme/api/-/merge_requests/7"
        )

    def test_none_when_it_published_none(self, admin_user):
        coordinator = make_coordinator(admin_user)
        leg_run = make_leg(coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL)

        assert Leg(leg_run.session).merge_request() is None
        assert Leg(leg_run.session).merge_request_state() == MergeRequestState.OPEN


@pytest.mark.django_db
class TestMany:
    def test_it_reads_what_each_leg_reads_on_its_own_in_three_queries(self, admin_user, django_assert_num_queries):
        coordinator = make_coordinator(admin_user)
        pushed = make_leg(
            coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL, ref="", session_ref="daiv/a", pushed_sha="abc123"
        )
        published = make_leg(
            coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL, ref="release", session_ref="daiv/b",
            merge_request_iid=4, merge_request_web_url="https://gitlab.example/acme/api/-/merge_requests/4",
        )  # fmt: skip
        _follow_up_run(published, status=RunStatus.SUCCESSFUL, merge_request_iid=7)
        untouched = make_leg(coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL, ref="", session_ref="main")
        sessions = [run.session for run in (pushed, published, untouched)]
        expected = {
            leg.session.pk: (leg.base_ref, leg.has_own_branch(), leg.merge_request()) for leg in map(Leg, sessions)
        }

        with django_assert_num_queries(3):
            legs = Leg.many(sessions)
            loaded = {pk: (leg.base_ref, leg.has_own_branch(), leg.merge_request()) for pk, leg in legs.items()}

        assert loaded == expected
        assert expected[published.session_id][2] == LegMergeRequest("acme/api", 7, "")


@pytest.mark.django_db(transaction=True)
class TestMergeRequestState:
    def _owned_leg(self, admin_user) -> Run:
        coordinator = make_coordinator(admin_user)
        leg_run = make_leg(coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL, merge_request_iid=4)
        MergeRequestOwnership.objects.create(
            repo_id="acme/api", merge_request_iid=4, session=leg_run.session, source_branch="daiv/leg-branch"
        )
        return leg_run

    async def test_open_while_owned_and_not_closed(self, admin_user):
        leg_run = await sync_to_async(self._owned_leg)(admin_user)

        assert await sync_to_async(Leg(leg_run.session).merge_request_state)() == MergeRequestState.OPEN

    @pytest.mark.parametrize(
        ("merged", "state"),
        [(True, MergeRequestState.MERGED), (False, MergeRequestState.CLOSED)],
        ids=["merged", "closed"],
    )
    async def test_merged_or_closed_once_ownership_closes_it(self, admin_user, merged, state):
        leg_run = await sync_to_async(self._owned_leg)(admin_user)

        await Ownerships("acme/api").aclose(4, merged=merged, by_username="sfr", target_branch="main")

        assert await sync_to_async(Leg(leg_run.session).merge_request_state)() == state


@pytest.mark.django_db
class TestBusyLostArchived:
    @pytest.mark.parametrize("status", [RunStatus.QUEUED, RunStatus.READY, RunStatus.RUNNING])
    def test_an_unfinished_delegated_run_makes_it_busy(self, admin_user, status):
        coordinator = make_coordinator(admin_user)
        leg_run = make_leg(coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL)
        _follow_up_run(leg_run, status=status)

        assert Leg(leg_run.session).is_busy()

    def test_a_leg_waiting_for_its_coordinator_is_busy(self, admin_user):
        coordinator = make_coordinator(admin_user)
        delegating = make_delegating_run(coordinator, admin_user)
        leg_run = make_leg(coordinator, uuid.uuid4(), status=RunStatus.QUEUED, waits_for=delegating)

        assert Leg(leg_run.session).is_busy()

    def test_a_leg_whose_delegated_runs_all_ended_is_not_busy(self, admin_user):
        coordinator = make_coordinator(admin_user)
        leg_run = make_leg(coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL)
        _follow_up_run(leg_run, status=RunStatus.FAILED)
        Run.objects.create(
            session=leg_run.session, trigger_type=SessionOrigin.CHAT, repo_id="acme/api", status=RunStatus.RUNNING
        )

        assert not Leg(leg_run.session).is_busy()

    def test_lost_only_when_its_latest_delegated_run_could_not_be_linked(self, admin_user):
        coordinator = make_coordinator(admin_user)
        leg_run = make_leg(coordinator, uuid.uuid4(), status=RunStatus.FAILED, error_message=f"{LINK_FAILED_PREFIX}: x")

        assert Leg(leg_run.session).is_lost()

        _follow_up_run(leg_run, status=RunStatus.FAILED, error_message="enqueue_failed: x")

        assert not Leg(leg_run.session).is_lost()

    def test_archived_when_its_session_is(self, admin_user):
        coordinator = make_coordinator(admin_user)
        session = make_leg(coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL).session
        before = Leg(session).is_archived()
        Session.objects.filter(pk=session.pk).update(archived_at=timezone.now())
        session.refresh_from_db()

        assert (before, Leg(session).is_archived()) == (False, True)
