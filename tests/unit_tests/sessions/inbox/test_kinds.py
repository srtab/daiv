import uuid

import pytest
from sessions.inbox import kinds
from sessions.inbox.kinds import EventContent
from sessions.models import EventKind, SessionOrigin

CI = {"pipeline_id": 1, "sha": "abc", "status": "failed", "web_url": "https://ci/1", "failed_jobs": ["tests"]}


class TestValidatePayload:
    def test_a_complete_payload_passes(self):
        EventContent(EventKind.CI_FAILED, CI).validate_payload()

    def test_a_missing_required_key_raises(self):
        payload = {key: value for key, value in CI.items() if key != "sha"}

        with pytest.raises(ValueError, match="sha"):
            EventContent(EventKind.CI_FAILED, payload).validate_payload()

    def test_optional_keys_may_be_left_out(self):
        EventContent(
            EventKind.MENTION, {"note_id": 5, "body": "@daiv look", "web_url": "https://mr/9"}
        ).validate_payload()

    def test_a_mention_may_carry_the_model_its_label_asked_for(self):
        assert {"agent_model", "agent_thinking_level"} <= kinds.CommentPayload.__optional_keys__

    def test_a_foreign_publish_may_name_and_link_the_publishing_session(self):
        assert {"title", "web_url"} <= kinds.ForeignPublishPayload.__optional_keys__

    def test_an_unknown_kind_raises(self):
        with pytest.raises(ValueError, match="unknown"):
            EventContent("unknown", {}).validate_payload()

    def test_a_non_mapping_payload_raises(self):
        with pytest.raises(ValueError, match="mapping"):
            EventContent(EventKind.NOTICE, ["text"]).validate_payload()


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
        assert EventContent(kind, {}).run_trigger_type() == trigger

    @pytest.mark.parametrize("trigger", [SessionOrigin.API_JOB, SessionOrigin.MCP_JOB])
    def test_a_follow_up_job_runs_as_the_trigger_it_was_submitted_through(self, trigger):
        assert kinds.PRECEDENCE[EventKind.FOLLOW_UP_JOB] == 3
        assert EventContent(EventKind.FOLLOW_UP_JOB, {"trigger_type": trigger}).run_trigger_type() == trigger

    def test_a_follow_up_job_without_a_trigger_runs_as_an_api_job(self):
        assert EventContent(EventKind.FOLLOW_UP_JOB, {}).run_trigger_type() == SessionOrigin.API_JOB

    def test_a_remembered_kind_never_wakes(self):
        assert EventKind.CI_PASSED not in kinds.WAKING_KINDS
        with pytest.raises(ValueError, match="never wakes"):
            EventContent(EventKind.CI_PASSED, {}).run_trigger_type()

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

        assert kinds.SourceKey.pipeline(830724) == "pipeline:830724"
        assert kinds.SourceKey.note(5521) == "note:5521"
        assert kinds.SourceKey.review_comment(17) == "review-comment:17"
        assert kinds.SourceKey.batch(batch) == f"batch:{batch}"
        assert kinds.SourceKey.chat("msg-1") == "chat:msg-1"
        assert kinds.SourceKey.job(batch) == f"job:{batch}"
        assert kinds.SourceKey.push("e96f1f77") == "push:e96f1f77"
        assert kinds.SourceKey.mr_state(9, "merged") == "mr-state:9:merged"
        assert kinds.SourceKey.child_ci("thread-1", 830724, "no_push") == "child-ci:thread-1:830724:no_push"

    def test_subjects(self):
        assert kinds.Subject.ci(9) == "ci:9"
        assert kinds.Subject.ci(9, workflow_id=11) == "ci:9:11"
        assert kinds.Subject.mr_state(9) == "mr-state:9"
        assert kinds.Subject.branch("chore/x") == "branch:chore/x"
        assert kinds.Subject.child_ci("thread-1", 9) == "child-ci:thread-1:9"

    def test_a_key_longer_than_the_column_is_cut(self):
        assert len(kinds.Subject.branch("b" * 400)) == 255
