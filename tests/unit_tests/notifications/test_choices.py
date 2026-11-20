from notifications.choices import EnvelopeStatus


def test_a_question_ranks_below_a_failure_and_above_findings():
    assert EnvelopeStatus.worst_first() == (
        EnvelopeStatus.FAILED,
        EnvelopeStatus.NEEDS_INPUT,
        EnvelopeStatus.FOUND_ISSUES,
        EnvelopeStatus.NEEDS_ATTENTION,
        EnvelopeStatus.ALL_CLEAR,
    )


def test_every_deduped_event_type_is_in_the_unique_constraint():
    """A notice keyed per source must be exactly-once; the DB constraint only covers the types it names."""
    from notifications.choices import EventType
    from notifications.models import Notification

    [constraint] = Notification._meta.constraints
    covered = set(dict(constraint.condition.children)["event_type__in"])

    assert EventType.INBOX_PAUSED in covered
