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
                SessionOrigin.SUBMODULE_MERGE,
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


class TestLegFollowUp:
    PAYLOAD = {"prompt": "Make the failing test pass", "batch_id": "b"}

    def test_its_payload_needs_a_prompt_and_a_batch(self):
        EventContent(EventKind.LEG_FOLLOW_UP, self.PAYLOAD | {"references": [], "muted": True}).validate_payload()
        for key in self.PAYLOAD:
            payload = {name: value for name, value in self.PAYLOAD.items() if name != key}
            with pytest.raises(ValueError, match=key):
                EventContent(EventKind.LEG_FOLLOW_UP, payload).validate_payload()

    def test_it_wakes_ranks_with_a_follow_up_job_and_is_neither_gated_nor_a_persons(self):
        assert EventKind.LEG_FOLLOW_UP in kinds.WAKING_KINDS
        assert kinds.PRECEDENCE[EventKind.LEG_FOLLOW_UP] == kinds.PRECEDENCE[EventKind.FOLLOW_UP_JOB]
        assert EventKind.LEG_FOLLOW_UP not in kinds.GATED_KINDS
        assert EventKind.LEG_FOLLOW_UP not in kinds.PERSON_KINDS

    def test_it_follows_the_session_and_runs_as_a_delegated_run_outside_the_inbox_triggers(self):
        content = EventContent(EventKind.LEG_FOLLOW_UP, self.PAYLOAD)

        assert content.follows_session()
        assert content.run_trigger_type() == SessionOrigin.DELEGATED_JOB
        assert SessionOrigin.DELEGATED_JOB not in kinds.INBOX_TRIGGERS

    def test_its_source_key_is_one_per_batch(self):
        batch = uuid.uuid4()

        assert kinds.SourceKey.leg_follow_up(batch) == f"leg-follow-up:{batch}"


class TestMergeRequestJob:
    PAYLOAD = {"prompt": "/code-review", "trigger_type": SessionOrigin.MCP_JOB}

    def test_its_payload_needs_a_prompt_and_its_trigger_type(self):
        EventContent(EventKind.MERGE_REQUEST_JOB, self.PAYLOAD | {"muted": True}).validate_payload()
        for key in self.PAYLOAD:
            payload = {name: value for name, value in self.PAYLOAD.items() if name != key}
            with pytest.raises(ValueError, match=key):
                EventContent(EventKind.MERGE_REQUEST_JOB, payload).validate_payload()

    def test_it_wakes_as_a_persons_request_with_a_run_of_its_own(self):
        assert EventKind.MERGE_REQUEST_JOB in kinds.WAKING_KINDS
        assert EventKind.MERGE_REQUEST_JOB in kinds.PERSON_KINDS
        assert EventKind.MERGE_REQUEST_JOB in kinds.OWN_RUN_KINDS
        assert EventKind.MERGE_REQUEST_JOB not in kinds.GATED_KINDS

    @pytest.mark.parametrize(
        ("source", "trigger"),
        [
            (SessionOrigin.MCP_JOB, SessionOrigin.MCP_JOB),
            (SessionOrigin.API_JOB, SessionOrigin.API_JOB),
            ("x", "api_job"),
        ],
    )
    def test_its_run_takes_the_trigger_type_of_its_source(self, source, trigger):
        content = EventContent(EventKind.MERGE_REQUEST_JOB, self.PAYLOAD | {"trigger_type": source})

        assert content.run_trigger_type() == trigger
        assert not content.follows_session()


class TestSubmoduleMerged:
    PAYLOAD = {
        "path": "libs/core",
        "root_repo_id": "group/app",
        "merge_request_iid": 12,
        "recorded_sha": "9f8e7d6",
        "merged_sha": "1a2b3c4",
    }

    def test_its_payload_needs_every_field(self):
        EventContent(EventKind.SUBMODULE_MERGED, self.PAYLOAD).validate_payload()
        for key in self.PAYLOAD:
            payload = {name: value for name, value in self.PAYLOAD.items() if name != key}
            with pytest.raises(ValueError, match=key):
                EventContent(EventKind.SUBMODULE_MERGED, payload).validate_payload()

    def test_it_wakes_after_a_ci_failure_ungated_as_daivs_own_run(self):
        assert EventKind.SUBMODULE_MERGED in kinds.WAKING_KINDS
        assert kinds.PRECEDENCE[EventKind.SUBMODULE_MERGED] > kinds.PRECEDENCE[EventKind.CI_FAILED]
        assert EventKind.SUBMODULE_MERGED not in kinds.GATED_KINDS
        assert EventKind.SUBMODULE_MERGED not in kinds.PERSON_KINDS

    def test_its_run_is_a_submodule_merge(self):
        content = EventContent(EventKind.SUBMODULE_MERGED, self.PAYLOAD)

        assert content.run_trigger_type() == SessionOrigin.SUBMODULE_MERGE
        assert not content.follows_session()

    def test_its_source_key_is_one_per_merge_request(self):
        assert kinds.SourceKey.submodule_merged("group/core", 12) == "submodule_merged:group/core!12"
