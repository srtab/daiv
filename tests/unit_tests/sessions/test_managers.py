import uuid
from unittest.mock import MagicMock, patch

from django.db.models import Q
from django.test import TestCase

import pytest
from asgiref.sync import sync_to_async
from sessions.models import Run, RunArtifact, RunStatus, Session, SessionOrigin
from sessions.signal_defs import run_finished

from core.constants import CROSS_PROJECT_SESSION_REFUSED_MESSAGE
from tests.unit_tests.sessions.conftest import grant_read_access, make_artifact


def _create_run(*, status: str = RunStatus.RUNNING, trigger_type: str = SessionOrigin.PIPELINE_WEBHOOK) -> Run:
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=trigger_type, repo_id="acme/api")
    return Run.objects.create(session=session, trigger_type=trigger_type, repo_id="acme/api", status=status)


@pytest.mark.django_db
class TestFinish:
    def test_it_writes_the_status_and_fields_and_updates_the_instance(self, run_finished_spy, badge):
        run = _create_run()

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert Run.objects.finish(run, RunStatus.FAILED, error_message="boom") is True

        assert (run.status, run.error_message) == (RunStatus.FAILED, "boom")
        run.refresh_from_db()
        assert (run.status, run.error_message) == (RunStatus.FAILED, "boom")
        badge.assert_called_once()
        assert run_finished_spy.call_args.kwargs["run"] is run
        assert run_finished_spy.call_args.kwargs["skip_dispatch"] is False
        assert run_finished_spy.call_args.kwargs["never_started"] is False

    def test_the_badge_refresh_waits_for_the_commit(self, badge):
        run = _create_run()

        with TestCase.captureOnCommitCallbacks(execute=False) as callbacks:
            Run.objects.finish(run, RunStatus.SUCCESSFUL)
        badge.assert_not_called()
        for callback in callbacks:
            callback()
        badge.assert_called_once()

    def test_it_forwards_the_emit_flags(self, run_finished_spy):
        run = _create_run()

        Run.objects.finish(run, RunStatus.FAILED, skip_dispatch=True, never_started=True)

        assert run_finished_spy.call_args.kwargs["skip_dispatch"] is True
        assert run_finished_spy.call_args.kwargs["never_started"] is True

    def test_a_chat_run_refreshes_the_badge_without_emitting(self, run_finished_spy, badge):
        run = _create_run(trigger_type=SessionOrigin.CHAT)

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert Run.objects.finish(run, RunStatus.SUCCESSFUL) is True

        badge.assert_called_once()
        assert not run_finished_spy.called

    def test_a_finished_row_is_never_overwritten(self, run_finished_spy, badge):
        run = _create_run()
        Run.objects.filter(pk=run.pk).update(status=RunStatus.SUCCESSFUL)

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert Run.objects.finish(run, RunStatus.FAILED, error_message="late") is False

        run.refresh_from_db()
        assert (run.status, run.error_message) == (RunStatus.SUCCESSFUL, "")
        badge.assert_not_called()
        assert not run_finished_spy.called

    def test_a_row_failing_the_guard_is_left_alone(self, run_finished_spy):
        run = _create_run(status=RunStatus.RUNNING)

        assert Run.objects.finish(run, RunStatus.FAILED, where=Q(status=RunStatus.READY)) is False

        assert run.status == RunStatus.RUNNING
        run.refresh_from_db()
        assert run.status == RunStatus.RUNNING
        assert not run_finished_spy.called

    def test_a_non_terminal_status_is_refused(self):
        run = _create_run(status=RunStatus.READY)

        with pytest.raises(ValueError, match="not a terminal run status"):
            Run.objects.finish(run, RunStatus.RUNNING)

    def test_a_failing_receiver_neither_stops_the_others_nor_the_finish(self):
        def _broken(sender, run, **kwargs):
            raise RuntimeError("receiver down")

        later = MagicMock()
        run_finished.connect(_broken, dispatch_uid="test-managers-broken")
        run_finished.connect(later, dispatch_uid="test-managers-later")
        try:
            run = _create_run()
            assert Run.objects.finish(run, RunStatus.SUCCESSFUL) is True
        finally:
            run_finished.disconnect(dispatch_uid="test-managers-broken")
            run_finished.disconnect(dispatch_uid="test-managers-later")

        later.assert_called_once()
        run.refresh_from_db()
        assert run.status == RunStatus.SUCCESSFUL


@pytest.mark.django_db
class TestUpdateUnfinished:
    def test_a_finished_row_is_left_alone(self, badge):
        run = _create_run(status=RunStatus.SUCCESSFUL)

        assert Run.objects.update_unfinished(run, status=RunStatus.RUNNING) is False

        run.refresh_from_db()
        assert run.status == RunStatus.SUCCESSFUL
        badge.assert_not_called()

    def test_a_terminal_status_is_refused(self):
        run = _create_run(status=RunStatus.RUNNING)

        with pytest.raises(ValueError, match="is a terminal run status"):
            Run.objects.update_unfinished(run, status=RunStatus.FAILED)


@pytest.mark.django_db(transaction=True)
class TestAfinish:
    async def test_it_writes_refreshes_the_badge_and_emits(self, run_finished_spy):
        run = await sync_to_async(_create_run)()

        with patch("core.ui_events.publisher.runs_changed") as publish:
            assert await Run.objects.afinish(run, RunStatus.WAITING_INPUT, question={"q": 1}) is True

        await run.arefresh_from_db()
        assert (run.status, run.question) == (RunStatus.WAITING_INPUT, {"q": 1})
        publish.assert_called_once()
        assert run_finished_spy.call_args.kwargs["run"] is run

    async def test_a_chat_run_refreshes_the_badge_without_emitting(self, run_finished_spy):
        run = await sync_to_async(_create_run)(trigger_type=SessionOrigin.CHAT)

        with patch("core.ui_events.publisher.runs_changed") as publish:
            assert await Run.objects.afinish(run, RunStatus.FAILED) is True

        publish.assert_called_once()
        assert not run_finished_spy.called

    async def test_a_finished_row_is_never_overwritten(self, run_finished_spy):
        run = await sync_to_async(_create_run)()
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.FAILED)

        with patch("core.ui_events.publisher.runs_changed") as publish:
            assert await Run.objects.afinish(run, RunStatus.SUCCESSFUL) is False

        publish.assert_not_called()
        assert not run_finished_spy.called


def _session(**kwargs) -> Session:
    defaults = {"thread_id": str(uuid.uuid4()), "origin": SessionOrigin.API_JOB, "repo_id": "group/repo"}
    return Session.objects.create(**(defaults | kwargs))


def _run(session: Session, **kwargs) -> Run:
    defaults = {"trigger_type": SessionOrigin.ISSUE_WEBHOOK, "repo_id": session.repo_id, "status": RunStatus.SUCCESSFUL}
    return Run.objects.create(session=session, **(defaults | kwargs))


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
    grant_read_access(other_user, session.repo_id)
    return session


@pytest.mark.django_db
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

    def test_the_cross_project_recheck_applies_the_mark_alone(self, shared_session, fetcher, other_user, admin_user):
        assert shared_session in Session.objects.cross_project_visible_to(other_user)

        Session.objects.filter(pk=shared_session.pk).update(cross_project_user_ids=[fetcher.pk])

        assert shared_session in Session.objects.cross_project_visible_to(fetcher)
        assert shared_session in Session.objects.cross_project_visible_to(admin_user)
        assert shared_session not in Session.objects.cross_project_visible_to(other_user)


@pytest.mark.django_db
class TestRunAndArtifactVisibility:
    def test_runs_and_artifacts_of_a_restricted_session_follow_it(self, shared_session, fetcher, other_user):
        theirs = shared_session.runs.get(user=other_user)
        artifact = make_artifact(theirs)
        assert theirs in Run.objects.visible_to(other_user)
        assert artifact in RunArtifact.objects.visible_to(other_user)

        Session.objects.filter(pk=shared_session.pk).update(cross_project_user_ids=[fetcher.pk])

        assert theirs not in Run.objects.visible_to(other_user)
        assert artifact not in RunArtifact.objects.visible_to(other_user)
        assert shared_session.runs.get(user=fetcher) in Run.objects.visible_to(fetcher)
        assert artifact in RunArtifact.objects.visible_to(fetcher)

    def test_a_schedule_subscriber_loses_the_runs(self, fetcher, other_user):
        session = _session(user=fetcher, scheduled_job=_subscribed_schedule(fetcher, other_user))
        run = _run(session, user=fetcher, trigger_type=SessionOrigin.SCHEDULE)

        Session.objects.filter(pk=session.pk).update(cross_project_user_ids=[fetcher.pk])

        assert run not in Run.objects.visible_to(other_user)


@pytest.mark.django_db
class TestRequesterResults:
    def test_a_requester_reads_their_own_runs_of_an_unrestricted_session(self, other_user):
        run = _run(_session(), user=other_user, trigger_type=SessionOrigin.API_JOB)

        assert list(Run.objects.results_for(other_user)) == [run]

    def test_a_restricted_session_hides_the_requesters_earlier_run(self, shared_session, fetcher, other_user):
        Session.objects.filter(pk=shared_session.pk).update(cross_project_user_ids=[fetcher.pk])

        assert not Run.objects.results_for(other_user).exists()
        assert Run.objects.results_for(fetcher).count() == 1

    def test_the_run_refused_at_start_stays_readable_to_its_requester(self, shared_session, fetcher, other_user):
        Session.objects.filter(pk=shared_session.pk).update(cross_project_user_ids=[fetcher.pk])
        refused = _run(
            shared_session,
            user=other_user,
            trigger_type=SessionOrigin.API_JOB,
            status=RunStatus.FAILED,
            error_message=CROSS_PROJECT_SESSION_REFUSED_MESSAGE,
        )

        assert list(Run.objects.results_for(other_user)) == [refused]

    def test_an_admin_reads_only_their_own_runs_as_before(self, shared_session, fetcher, admin_user):
        Session.objects.filter(pk=shared_session.pk).update(cross_project_user_ids=[fetcher.pk])
        own = _run(_session(), user=admin_user, trigger_type=SessionOrigin.API_JOB)

        assert list(Run.objects.results_for(admin_user)) == [own]
