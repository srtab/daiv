import uuid

import pytest
from notifications.choices import EnvelopeStatus, EventType
from notifications.models import Notification, RunEnvelope
from notifications.outcomes import CANT_RUN, CI_STILL_FAILING, ENVELOPE_OUTCOMES, attach_outcomes, outcome_for
from notifications.policy import SOURCE_BATCH, SOURCE_RUN
from sessions.models import Run, RunStatus, Session, SessionOrigin


def _notification(event_type: str, **kwargs) -> Notification:
    return Notification(event_type=event_type, subject="s", body="b", **kwargs)


def _run() -> Run:
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="g/r")
    return Run.objects.create(
        session=session, trigger_type=SessionOrigin.UI_JOB, repo_id="g/r", status=RunStatus.SUCCESSFUL
    )


@pytest.mark.parametrize("status", list(EnvelopeStatus))
@pytest.mark.parametrize("event_type", [EventType.JOB_FINISHED, EventType.SCHEDULE_FINISHED])
def test_a_run_notification_shows_its_envelope_status(event_type, status):
    outcome = outcome_for(_notification(event_type, context={"status_tone": "warning"}), status)
    assert outcome.key == status


def test_every_envelope_status_has_a_distinct_icon():
    assert len({outcome.icon for outcome in ENVELOPE_OUTCOMES.values()}) == len(EnvelopeStatus)


@pytest.mark.parametrize(
    ("tone", "expected"),
    [
        ("failure", EnvelopeStatus.FAILED),
        ("warning", EnvelopeStatus.NEEDS_ATTENTION),
        ("success", EnvelopeStatus.ALL_CLEAR),
        (None, EnvelopeStatus.NEEDS_ATTENTION),
    ],
)
def test_a_run_notification_without_its_envelope_falls_back_to_its_tone(tone, expected):
    assert outcome_for(_notification(EventType.JOB_FINISHED, context={"status_tone": tone})).key == expected


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({"failed_count": 1, "needs_input_count": 2, "found_count": 3}, EnvelopeStatus.FAILED),
        ({"needs_input_count": 1, "found_count": 3}, EnvelopeStatus.NEEDS_INPUT),
        ({"found_count": 1, "needs_attention_count": 4}, EnvelopeStatus.FOUND_ISSUES),
        ({"needs_attention_count": 2, "all_clear_count": 5}, EnvelopeStatus.NEEDS_ATTENTION),
    ],
)
def test_a_batch_shows_the_worst_status_any_run_reached(counts, expected):
    context = {"failed_count": 0, "needs_input_count": 0, "found_count": 0, "needs_attention_count": 0, **counts}
    assert outcome_for(_notification(EventType.JOB_BATCH_FINISHED, context=context)).key == expected


def test_the_watch_and_dispatch_notices_have_their_own_outcomes():
    assert outcome_for(_notification(EventType.PIPELINE_WATCH_EXHAUSTED)) == CI_STILL_FAILING
    assert outcome_for(_notification(EventType.SCHEDULE_DISPATCH_FAILED)) == CANT_RUN
    assert CI_STILL_FAILING.tone == CANT_RUN.tone == "fail"


@pytest.mark.django_db
def test_attach_outcomes_reads_each_runs_envelope_in_one_query(member_user, django_assert_num_queries):
    found, waiting, gone = _run(), _run(), _run()
    RunEnvelope.objects.create(run=found, status=EnvelopeStatus.FOUND_ISSUES, count=2)
    RunEnvelope.objects.create(run=waiting, status=EnvelopeStatus.NEEDS_INPUT)
    common = {"recipient": member_user, "source_type": SOURCE_RUN, "context": {"status_tone": "warning"}}
    notifications = [
        _notification(EventType.JOB_FINISHED, source_id=str(found.pk), **common),
        _notification(EventType.SCHEDULE_FINISHED, source_id=str(waiting.pk), **common),
        _notification(EventType.JOB_FINISHED, source_id=str(gone.pk), **common),
        _notification(
            EventType.JOB_BATCH_FINISHED,
            recipient=member_user,
            source_type=SOURCE_BATCH,
            source_id=str(uuid.uuid4()),
            context={"failed_count": 1},
        ),
    ]

    with django_assert_num_queries(1):
        attach_outcomes(notifications)

    assert [n.outcome.key for n in notifications] == [
        EnvelopeStatus.FOUND_ISSUES,
        EnvelopeStatus.NEEDS_INPUT,
        EnvelopeStatus.NEEDS_ATTENTION,
        EnvelopeStatus.FAILED,
    ]
