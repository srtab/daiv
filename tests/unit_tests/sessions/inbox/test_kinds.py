import uuid

import pytest
from sessions.inbox import kinds
from sessions.models import EventKind, SessionOrigin

CI = {"pipeline_id": 1, "sha": "abc", "status": "failed", "web_url": "https://ci/1", "failed_jobs": ["tests"]}


class TestValidatePayload:
    def test_a_complete_payload_passes(self):
        kinds.validate_payload(EventKind.CI_FAILED, CI)

    def test_a_missing_required_key_raises(self):
        payload = {key: value for key, value in CI.items() if key != "sha"}

        with pytest.raises(ValueError, match="sha"):
            kinds.validate_payload(EventKind.CI_FAILED, payload)

    def test_optional_keys_may_be_left_out(self):
        kinds.validate_payload(EventKind.MENTION, {"note_id": 5, "body": "@daiv look", "web_url": "https://mr/9"})

    def test_an_unknown_kind_raises(self):
        with pytest.raises(ValueError, match="unknown"):
            kinds.validate_payload("unknown", {})

    def test_a_non_mapping_payload_raises(self):
        with pytest.raises(ValueError, match="mapping"):
            kinds.validate_payload(EventKind.NOTICE, ["text"])


class TestWakingKinds:
    @pytest.mark.parametrize(
        ("kind", "precedence", "trigger"),
        [
            (EventKind.CHAT_MESSAGE, 1, SessionOrigin.CHAT),
            (EventKind.MENTION, 2, SessionOrigin.MR_WEBHOOK),
            (EventKind.DELEGATION_FINISHED, 4, SessionOrigin.DELEGATION_RESUME),
            (EventKind.CI_FAILED, 5, SessionOrigin.PIPELINE_WEBHOOK),
        ],
    )
    def test_each_waking_kind_has_its_precedence_and_trigger(self, kind, precedence, trigger):
        assert kinds.PRECEDENCE[kind] == precedence
        assert kinds.run_trigger_type(kind, {}) == trigger

    @pytest.mark.parametrize("trigger", [SessionOrigin.API_JOB, SessionOrigin.MCP_JOB])
    def test_a_follow_up_job_runs_as_the_trigger_it_was_submitted_through(self, trigger):
        assert kinds.PRECEDENCE[EventKind.FOLLOW_UP_JOB] == 3
        assert kinds.run_trigger_type(EventKind.FOLLOW_UP_JOB, {"trigger_type": trigger}) == trigger

    def test_a_follow_up_job_without_a_trigger_runs_as_an_api_job(self):
        assert kinds.run_trigger_type(EventKind.FOLLOW_UP_JOB, {}) == SessionOrigin.API_JOB

    def test_a_remembered_kind_never_wakes(self):
        assert EventKind.CI_PASSED not in kinds.WAKING_KINDS
        with pytest.raises(ValueError, match="never wakes"):
            kinds.run_trigger_type(EventKind.CI_PASSED, {})

    def test_the_inbox_triggers_are_the_waking_kinds_triggers(self):
        assert (
            frozenset({
                SessionOrigin.CHAT,
                SessionOrigin.MR_WEBHOOK,
                SessionOrigin.API_JOB,
                SessionOrigin.MCP_JOB,
                SessionOrigin.DELEGATION_RESUME,
                SessionOrigin.PIPELINE_WEBHOOK,
            })
            == kinds.INBOX_TRIGGERS
        )


class TestKeys:
    def test_source_keys(self):
        batch = uuid.uuid4()

        assert kinds.pipeline_key(830724) == "pipeline:830724"
        assert kinds.note_key(5521) == "note:5521"
        assert kinds.review_comment_key(17) == "review-comment:17"
        assert kinds.batch_key(batch) == f"batch:{batch}"
        assert kinds.chat_key("msg-1") == "chat:msg-1"
        assert kinds.job_key(batch) == f"job:{batch}"
        assert kinds.push_key("e96f1f77") == "push:e96f1f77"
        assert kinds.mr_state_key(9, "merged") == "mr-state:9:merged"
        assert kinds.child_ci_key("thread-1", 830724) == "child-ci:thread-1:830724"

    def test_subjects(self):
        assert kinds.ci_subject(9) == "ci:9"
        assert kinds.mr_state_subject(9) == "mr-state:9"
        assert kinds.branch_subject("chore/x") == "branch:chore/x"
        assert kinds.child_ci_subject("thread-1", 9) == "child-ci:thread-1:9"

    def test_a_key_longer_than_the_column_is_cut(self):
        assert len(kinds.branch_subject("b" * 400)) == 255
