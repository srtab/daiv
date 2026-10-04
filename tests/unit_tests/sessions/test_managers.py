import uuid

from django.utils import timezone

import pytest
from allauth.socialaccount.models import SocialAccount
from sessions.models import Run, RunArtifact, RunStatus, Session, SessionOrigin

from codebase.models import RepositoryAccess
from core.constants import CROSS_PROJECT_SESSION_REFUSED_MESSAGE
from tests.unit_tests.sessions.conftest import make_artifact

pytestmark = pytest.mark.django_db


def _session(**kwargs) -> Session:
    defaults = {"thread_id": str(uuid.uuid4()), "origin": SessionOrigin.API_JOB, "repo_id": "group/repo"}
    return Session.objects.create(**(defaults | kwargs))


def _run(session: Session, **kwargs) -> Run:
    defaults = {"trigger_type": SessionOrigin.ISSUE_WEBHOOK, "repo_id": session.repo_id, "status": RunStatus.SUCCESSFUL}
    return Run.objects.create(session=session, **(defaults | kwargs))


def _grant_read_access(user, repo_id: str) -> None:
    SocialAccount.objects.get_or_create(user=user, provider="gitlab", uid=str(user.pk))
    RepositoryAccess.objects.create(
        provider="gitlab",
        uid=str(user.pk),
        username=user.username,
        repo_id=repo_id,
        access_level="read",
        synced_at=timezone.now(),
    )


def _subscribed_schedule(owner, subscriber):
    from schedules.models import Frequency, ScheduledJob

    schedule = ScheduledJob.objects.create(
        user=owner,
        name="s",
        prompt="p",
        repos=[{"repo_id": "group/repo", "ref": ""}],
        frequency=Frequency.DAILY,
        time="12:00",
    )
    schedule.subscribers.add(subscriber)
    return schedule


@pytest.fixture
def fetcher(member_user):
    """The person whose grant fetched another project's content into the shared session."""
    return member_user


@pytest.fixture
def shared_session(fetcher, other_user):
    """A webhook session both people acted in, readable by ``other_user`` through the attached repo."""
    session = _session(origin=SessionOrigin.ISSUE_WEBHOOK)
    _run(session, user=fetcher)
    _run(session, user=other_user)
    _grant_read_access(other_user, session.repo_id)
    return session


class TestSessionVisibility:
    def test_a_session_without_cross_project_results_keeps_every_viewer(self, shared_session, fetcher, other_user):
        assert shared_session in Session.objects.visible_to(other_user)
        assert shared_session in Session.objects.by_owner(other_user)
        assert shared_session in Session.objects.visible_to(fetcher)

    def test_only_the_fetcher_sees_a_session_holding_their_results(self, shared_session, fetcher, other_user):
        Session.objects.filter(pk=shared_session.pk).update(cross_project_user_ids=[fetcher.pk])

        assert shared_session in Session.objects.visible_to(fetcher)
        assert shared_session in Session.objects.by_owner(fetcher)
        assert shared_session not in Session.objects.visible_to(other_user)
        assert shared_session not in Session.objects.by_owner(other_user)

    def test_an_admin_still_sees_it(self, shared_session, fetcher, admin_user):
        Session.objects.filter(pk=shared_session.pk).update(cross_project_user_ids=[fetcher.pk])

        assert shared_session in Session.objects.visible_to(admin_user)
        assert shared_session in Session.objects.by_owner(admin_user)

    def test_a_schedule_subscriber_loses_it(self, fetcher, other_user):
        session = _session(user=fetcher, scheduled_job=_subscribed_schedule(fetcher, other_user))
        assert session in Session.objects.by_owner(other_user)

        Session.objects.filter(pk=session.pk).update(cross_project_user_ids=[fetcher.pk])

        assert session not in Session.objects.by_owner(other_user)
        assert session not in Session.objects.visible_to(other_user)

    def test_an_unattributed_fetch_leaves_the_session_to_admins(self, shared_session, fetcher, admin_user):
        Session.objects.filter(pk=shared_session.pk).update(cross_project_user_ids=[None])

        assert shared_session not in Session.objects.visible_to(fetcher)
        assert shared_session in Session.objects.visible_to(admin_user)

    def test_another_id_that_merely_contains_the_viewers_digits_does_not_match(self, shared_session, fetcher):
        Session.objects.filter(pk=shared_session.pk).update(cross_project_user_ids=[int(f"{fetcher.pk}0")])

        assert shared_session not in Session.objects.visible_to(fetcher)


class TestRunAndArtifactVisibility:
    def test_runs_and_artifacts_of_a_restricted_session_follow_it(self, shared_session, fetcher, other_user):
        theirs = shared_session.runs.get(user=other_user)
        artifact = make_artifact(theirs)
        assert theirs in Run.objects.visible_to(other_user)
        assert theirs in Run.objects.by_owner(other_user)
        assert artifact in RunArtifact.objects.visible_to(other_user)

        Session.objects.filter(pk=shared_session.pk).update(cross_project_user_ids=[fetcher.pk])

        assert theirs not in Run.objects.visible_to(other_user)
        assert theirs not in Run.objects.by_owner(other_user)
        assert artifact not in RunArtifact.objects.visible_to(other_user)
        assert shared_session.runs.get(user=fetcher) in Run.objects.visible_to(fetcher)
        assert artifact in RunArtifact.objects.visible_to(fetcher)

    def test_a_schedule_subscriber_loses_the_runs(self, fetcher, other_user):
        session = _session(user=fetcher, scheduled_job=_subscribed_schedule(fetcher, other_user))
        run = _run(session, user=fetcher, trigger_type=SessionOrigin.SCHEDULE)

        Session.objects.filter(pk=session.pk).update(cross_project_user_ids=[fetcher.pk])

        assert run not in Run.objects.by_owner(other_user)


def _own_results(user):
    return Run.objects.filter(Run.objects.results_visible_q(user), user=user)


class TestRequesterResults:
    def test_a_requester_reads_their_own_runs_of_an_unrestricted_session(self, other_user):
        run = _run(_session(), user=other_user, trigger_type=SessionOrigin.API_JOB)

        assert list(_own_results(other_user)) == [run]

    def test_a_restricted_session_hides_the_requesters_earlier_run(self, shared_session, fetcher, other_user):
        Session.objects.filter(pk=shared_session.pk).update(cross_project_user_ids=[fetcher.pk])

        assert not _own_results(other_user).exists()
        assert _own_results(fetcher).count() == 1

    def test_the_run_refused_at_start_stays_readable_to_its_requester(self, shared_session, fetcher, other_user):
        Session.objects.filter(pk=shared_session.pk).update(cross_project_user_ids=[fetcher.pk])
        refused = _run(
            shared_session,
            user=other_user,
            trigger_type=SessionOrigin.API_JOB,
            status=RunStatus.FAILED,
            error_message=CROSS_PROJECT_SESSION_REFUSED_MESSAGE,
        )

        assert list(_own_results(other_user)) == [refused]

    def test_an_admin_reads_only_their_own_runs_as_before(self, shared_session, fetcher, admin_user):
        Session.objects.filter(pk=shared_session.pk).update(cross_project_user_ids=[fetcher.pk])
        own = _run(_session(), user=admin_user, trigger_type=SessionOrigin.API_JOB)

        assert list(_own_results(admin_user)) == [own]
