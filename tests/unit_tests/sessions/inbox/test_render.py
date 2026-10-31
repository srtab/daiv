from datetime import timedelta

from django.utils import timezone

import pytest
from sessions.inbox import kinds
from sessions.inbox.render import (
    CONTEXT_HEADER,
    CONTEXT_MAX_CHARS,
    MENTION_REPLY_NOTE,
    QUOTE_MAX_CHARS,
    arender_input,
    render_context,
    render_request,
)
from sessions.models import EventKind, EventState, InboxEvent, QuietReason, Run, SessionOrigin

from automation.agent.synthetic import synthetic_kind
from tests.unit_tests.sessions.inbox.conftest import amake_event, amake_session, ci_payload

_START = timezone.now() - timedelta(hours=1)


def _event(kind: str, payload: dict, *, minute: int = 0, **fields) -> InboxEvent:
    """An unsaved event; ``minute`` orders it."""
    defaults = {
        "kind": kind,
        "payload": payload,
        "wake": kind in kinds.WAKING_KINDS,
        "ref": "chore/x",
        "merge_request_iid": 9,
        "state": EventState.TAKEN,
        "created_at": _START + timedelta(minutes=minute),
    }
    return InboxEvent(**(defaults | fields))


def _comment(body: str, **fields) -> InboxEvent:
    return _event(
        EventKind.COMMENT, {"note_id": 1, "body": body, "web_url": "https://mr/9"}, author_username="alice", **fields
    )


class TestContext:
    def test_nothing_remembered_renders_nothing(self):
        assert render_context([_event(EventKind.CI_FAILED, ci_payload())]) == ""

    def test_lines_are_oldest_first(self):
        merged = _event(EventKind.MR_MERGED, {"target_branch": "main", "by_username": "sfr"}, minute=2)
        comment = _comment("Looks good once CI is green.", minute=1)

        text = render_context([comment, merged])

        assert text.splitlines() == [
            CONTEXT_HEADER,
            "",
            '- Comment by alice on !9 (not addressed to you): "Looks good once CI is green."',
            "- !9 was merged into main by sfr.",
        ]

    def test_a_superseded_event_is_shown_with_its_replacement_beneath_it(self):
        failed = _event(
            EventKind.CI_FAILED, ci_payload(830724), state=EventState.SUPERSEDED, subject=kinds.ci_subject(9)
        )
        passed = _event(
            EventKind.CI_PASSED, ci_payload(830731, status="success", jobs=()), minute=1, subject=kinds.ci_subject(9)
        )

        lines = render_context([failed, passed]).splitlines()[2:]

        assert lines == [
            "- CI on !9 (branch chore/x): pipeline 830724 FAILED on e96f1f77, jobs: build. "
            "https://gitlab.example/pipelines/830724",
            "  Superseded by pipeline 830731 on e96f1f77: PASSED.",
        ]

    def test_a_quiet_ci_failure_says_why_the_agent_was_not_woken(self):
        quiet = _event(EventKind.CI_FAILED, ci_payload(), wake=False, reason=QuietReason.ALREADY_EXAMINED)

        assert render_context([quiet]).endswith("You were not asked to act on it: you already examined this commit.")

    def test_a_comment_quote_is_cut(self):
        text = render_context([_comment("x" * (QUOTE_MAX_CHARS + 50))])

        assert f'"{"x" * QUOTE_MAX_CHARS}…"' in text

    def test_over_the_cap_the_oldest_remembered_lines_collapse(self):
        comments = [_comment("c" * 900, minute=minute) for minute in range(12)]
        waking_chain = [
            _event(EventKind.CI_FAILED, ci_payload(1), state=EventState.SUPERSEDED, subject="ci:9", minute=20),
            _event(EventKind.CI_FAILED, ci_payload(2), subject="ci:9", minute=21),
        ]

        text = render_context([*comments, *waking_chain])

        assert len(text) <= CONTEXT_MAX_CHARS
        collapsed = int(text.splitlines()[2].removeprefix("- ").split(" ")[0])
        assert text.splitlines()[2] == f"- {collapsed} more comment events"
        assert text.count("Comment by alice") == len(comments) - collapsed
        assert "Superseded by pipeline 2" in text


class TestFastForward:
    def _push(self) -> InboxEvent:
        payload = {"before": "e96f1f77aa", "after": "0a1b2c3dbb", "commit_count": 2, "pusher_username": "sfr"}
        return _event(EventKind.BRANCH_PUSHED, payload, subject="branch:chore/x")

    @pytest.mark.parametrize(
        ("result", "sentence"),
        [
            ("advanced", "Your working copy was fast-forwarded."),
            ("diverged", "Your working copy could not be fast-forwarded because you have unpublished commits."),
        ],
    )
    def test_the_branch_line_says_whether_the_working_copy_followed(self, result, sentence):
        text = render_context([self._push()], fast_forward=result, branch="chore/x")

        assert text.splitlines()[-1] == (
            f"- The branch chore/x moved: sfr pushed 2 commits (e96f1f77 → 0a1b2c3d). {sentence}"
        )

    @pytest.mark.parametrize("result", [None, "unchanged"])
    def test_nothing_is_said_when_the_working_copy_did_not_move(self, result):
        text = render_context([self._push()], fast_forward=result, branch="chore/x")

        assert text.splitlines()[-1].endswith("(e96f1f77 → 0a1b2c3d).")

    def test_a_move_with_no_push_event_gets_a_line_of_its_own(self):
        assert render_context([], fast_forward="advanced", branch="chore/x").splitlines()[2:] == [
            "- The branch chore/x moved on the remote. Your working copy was fast-forwarded."
        ]


class TestRequest:
    def test_a_ci_only_run_gets_the_fix_prompt(self):
        text = render_request([_event(EventKind.CI_FAILED, ci_payload(jobs=("lint", "tests")))])

        assert text.startswith("CI failed on this merge request. Failed jobs: lint, tests. Pipeline: https://")
        assert "explain why instead of guessing" in text

    def test_sections_follow_precedence(self):
        events = [
            _event(EventKind.CI_FAILED, ci_payload(), minute=0),
            _event(EventKind.DELEGATION_FINISHED, {"batch_id": "b", "summary": "Both jobs finished."}, minute=1),
            _event(EventKind.FOLLOW_UP_JOB, {"prompt": "Bump the version", "trigger_type": "api_job"}, minute=2),
            _event(EventKind.MENTION, {"note_id": 1, "body": "@daiv why?", "web_url": "https://mr/9#1"}, minute=3,
                   author_username="bob"),
            _event(EventKind.CHAT_MESSAGE, {"text": "Also add a test", "agui_message_id": "m"}, minute=4),
        ]  # fmt: skip

        sections = render_request(events).split("\n\n")

        assert sections[0] == "Also add a test"
        assert sections[1] == "bob mentioned you on !9 (https://mr/9#1):\n> @daiv why?"
        assert sections[2] == MENTION_REPLY_NOTE
        assert sections[3] == "Bump the version"
        assert sections[4] == "Both jobs finished."
        assert sections[5].startswith("CI failed on this merge request.")

    def test_several_chat_messages_carry_their_times(self):
        first = _event(EventKind.CHAT_MESSAGE, {"text": "one", "agui_message_id": "a"}, minute=0)
        second = _event(EventKind.CHAT_MESSAGE, {"text": "two", "agui_message_id": "b"}, minute=1)

        assert render_request([first, second]) == (f"[{first.created_at:%H:%M}] one\n\n[{second.created_at:%H:%M}] two")

    def test_a_mention_quote_is_cut(self):
        mention = _event(EventKind.MENTION, {"note_id": 1, "body": "m" * 2000, "web_url": "u"}, author_username="bob")

        assert f"> {'m' * QUOTE_MAX_CHARS}…" in render_request([mention])

    def test_remembered_and_superseded_events_are_not_requests(self):
        superseded = _event(EventKind.CI_FAILED, ci_payload(1), state=EventState.SUPERSEDED)
        quiet = _event(EventKind.CI_FAILED, ci_payload(2), wake=False)
        chat = _event(EventKind.CHAT_MESSAGE, {"text": "hi", "agui_message_id": "a"})

        assert render_request([superseded, quiet, chat]) == "hi"


@pytest.mark.django_db(transaction=True)
class TestRenderInput:
    async def test_the_context_is_a_hidden_synthetic_message_before_the_request(self):
        session = await amake_session()
        run = await Run.objects.acreate(
            session=session, trigger_type=SessionOrigin.CHAT, repo_id=session.repo_id, message_id="msg-7"
        )
        await amake_event(session, EventKind.CHAT_MESSAGE, run=run, state=EventState.TAKEN)
        await amake_event(session, EventKind.MR_MERGED, run=run, state=EventState.TAKEN, merge_request_iid=9)

        rendered = await arender_input(run)

        assert synthetic_kind(rendered.context) == "inbox"
        assert "!9 was merged into main" in rendered.context.content
        assert rendered.request.id == "msg-7"
        assert rendered.messages == (rendered.context, rendered.request)

    async def test_no_context_is_sent_when_there_is_nothing_to_report(self):
        session = await amake_session()
        run = await Run.objects.acreate(session=session, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id="r")
        await amake_event(session, EventKind.CI_FAILED, run=run, state=EventState.TAKEN)

        rendered = await arender_input(run)

        assert rendered.context is None
        assert rendered.messages == (rendered.request,)
