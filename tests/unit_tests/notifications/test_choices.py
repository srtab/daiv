from notifications.choices import EnvelopeStatus


def test_a_question_ranks_below_a_failure_and_above_findings():
    assert EnvelopeStatus.worst_first() == (
        EnvelopeStatus.FAILED,
        EnvelopeStatus.NEEDS_INPUT,
        EnvelopeStatus.FOUND_ISSUES,
        EnvelopeStatus.NEEDS_ATTENTION,
        EnvelopeStatus.ALL_CLEAR,
    )
