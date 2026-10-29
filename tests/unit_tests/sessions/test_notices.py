import logging

import pytest
from sessions.models import Run, RunStatus
from sessions.notices import RunNoticeRecorder

from automation.agent.notices import ModelFallbackNotice
from tests.unit_tests.sessions.conftest import make_run


def _notice(name: str) -> ModelFallbackNotice:
    return ModelFallbackNotice(
        target="subagent",
        name=name,
        configured_model="nosuch:model",
        served_model="openrouter:run/model",
        reason="unknown_provider",
    )


@pytest.fixture
def run(transactional_db):
    return make_run(session_id="notices-session", status=RunStatus.RUNNING)


@pytest.mark.django_db(transaction=True)
async def test_record_buffers_until_flushed(run):
    recorder = RunNoticeRecorder(run.pk)

    recorder.record(_notice("cr-tests"))

    assert (await Run.objects.aget(pk=run.pk)).notices == []
    await recorder.aflush()
    assert (await Run.objects.aget(pk=run.pk)).notices == [_notice("cr-tests").as_dict()]


@pytest.mark.django_db(transaction=True)
async def test_a_later_flush_keeps_the_notices_already_saved(run):
    recorder = RunNoticeRecorder(run.pk)
    recorder.record(_notice("cr-tests"))
    await recorder.aflush()

    recorder.record(_notice("code-review"))
    await recorder.aflush()

    assert (await Run.objects.aget(pk=run.pk)).notices == [
        _notice("cr-tests").as_dict(),
        _notice("code-review").as_dict(),
    ]


@pytest.mark.django_db(transaction=True)
async def test_a_recorder_with_no_run_stores_nothing_and_logs(run, caplog):
    recorder = RunNoticeRecorder(None)
    recorder.record(_notice("cr-tests"))

    with caplog.at_level(logging.INFO, logger="daiv.sessions"):
        await recorder.aflush()

    assert (await Run.objects.aget(pk=run.pk)).notices == []
    assert "cr-tests" in caplog.text
