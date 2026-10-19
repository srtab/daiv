import uuid
from unittest.mock import MagicMock

import pytest
from sessions.managers import emit_run_finished_if_terminal
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.signal_defs import run_finished


def _create_run(*, status: str) -> Run:
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id="acme/api")
    return Run.objects.create(session=session, trigger_type=SessionOrigin.API_JOB, repo_id="acme/api", status=status)


@pytest.fixture
def received():
    receiver = MagicMock()
    run_finished.connect(receiver, dispatch_uid="test-managers-received")
    yield receiver
    run_finished.disconnect(dispatch_uid="test-managers-received")


@pytest.mark.django_db
class TestEmitRunFinishedIfTerminal:
    def test_emitted_on_transition_to_successful(self, received):
        run = _create_run(status=RunStatus.RUNNING)
        run.status = RunStatus.SUCCESSFUL
        run.save()

        emit_run_finished_if_terminal(run, previous_status=RunStatus.RUNNING)

        assert received.call_args.kwargs["run"] is run

    def test_not_emitted_when_still_running(self, received):
        run = _create_run(status=RunStatus.RUNNING)

        emit_run_finished_if_terminal(run, previous_status=RunStatus.READY)

        assert not received.called

    def test_not_emitted_when_already_terminal(self, received):
        run = _create_run(status=RunStatus.SUCCESSFUL)

        emit_run_finished_if_terminal(run, previous_status=RunStatus.SUCCESSFUL)

        assert not received.called
