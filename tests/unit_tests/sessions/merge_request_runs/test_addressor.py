from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sessions.executor.lock import SessionLock, SessionLockTimeoutError
from sessions.merge_request_runs.addressor import CommentsAddressorManager
from sessions.models import Run, Session, SessionOrigin
from sessions.services import acreate_run

from automation.agent.agent_settings import resolve_agent_settings
from automation.agent.provider_errors import tag_provider_failure
from automation.agent.questions import render_questions
from automation.agent.validators import AgentConfigurationError
from codebase.base import DiffLocation, GitPlatform, MergeRequest, Note, NoteableType, User
from codebase.clients.base import CommentThread
from codebase.exceptions import CloneCredentialUnavailableError, CloneRefNotFoundError
from core.provider_failures import ProviderFailure
from core.site_settings import site_settings
from tests.unit_tests.conftest import SAMPLE_QUESTION_PAYLOAD, ask_user_question_messages, site_snapshot
from tests.unit_tests.sessions.conftest import active_holder
from tests.unit_tests.sessions.merge_request_runs.conftest import (
    addressor_agent,
    addressor_ctx,
    addressor_run,
    clone_raising,
)

_AUTHOR = User(id=1, username="alice")
_UNABLE = "An unexpected error occurred while working on this merge request."


def _merge_request(merge_request_id: int = 99, *, source_branch: str = "feature") -> MergeRequest:
    return MergeRequest(
        repo_id="owner/repo",
        merge_request_id=merge_request_id,
        source_branch=source_branch,
        target_branch="main",
        title="t",
        description="d",
        web_url=f"https://git.test/owner/repo/-/merge_requests/{merge_request_id}",
        author=_AUTHOR,
    )


@pytest.fixture
def mention(captured_client):
    captured_client.get_merge_request_note.return_value = _note(11, "please fix")
    return captured_client


def _note(note_id: int, body: str, *, author: str = "bob") -> Note:
    return Note(
        id=note_id,
        body=body,
        author=User(id=2, username=author),
        noteable_type=NoteableType.MERGE_REQUEST,
        system=False,
        resolvable=True,
    )


async def _review_session(thread_id: str, **fields) -> Session:
    return await Session.objects.acreate(
        thread_id=thread_id, origin=SessionOrigin.MR_WEBHOOK, repo_id="owner/repo", ref="feature", **fields
    )


async def _address(**kwargs):
    """``address_comments`` for mention ``c-1`` on MR 99 of ``owner/repo``; ``kwargs`` override."""
    return await CommentsAddressorManager.address_comments(
        **(
            {
                "repo_id": "owner/repo",
                "merge_request": _merge_request(),
                "mention_comment_id": "c-1",
                "mention_note_id": 11,
                "thread_id": "t-mr",
                "use_max": False,
            }
            | kwargs
        )
    )


class TestReviewAfterRunMatrix:
    @pytest.mark.django_db(transaction=True)
    async def test_it_waits_while_another_holder_has_the_session_slot(self, mention):
        """B13: the run waits for the slot, then runs holding it."""
        thread_id = str(uuid.uuid4())
        await _review_session(thread_id, active_run_id="chat-run")
        real_try_claim = SessionLock.try_claim
        holders: list[str | None] = []

        async def _try_claim(thread_id, holder_id):
            claimed = await real_try_claim(thread_id, holder_id)
            if not claimed:
                await SessionLock.release(thread_id, "chat-run")
            return claimed

        async def _invoke(*_args, **_kwargs):
            holders.append(await active_holder(thread_id))
            return {"messages": [AIMessage(content="done")]}

        with (
            addressor_run(addressor_agent(side_effect=_invoke), real_lock=True, ctx=addressor_ctx()),
            patch("sessions.executor.lock.LOCK_POLL_INTERVAL_S", 0.01),
            patch("sessions.executor.lock.SessionLock.try_claim", _try_claim),
        ):
            await _address(thread_id=thread_id)

        [holder] = holders
        assert holder.startswith("webhook-")
        assert await active_holder(thread_id) is None

    @pytest.mark.django_db(transaction=True)
    async def test_a_successful_run_neither_moves_the_ref_nor_claims_the_merge_request(self, mention):
        """The checkpoint names another published branch, so any ref sync would show on the session row."""
        session = await _review_session(str(uuid.uuid4()))
        agent = addressor_agent(
            return_value={"messages": [AIMessage(content="done")]},
            state_values={"merge_request": _merge_request(source_branch="daiv/published"), "published": True},
        )
        merge_request = _merge_request()

        with addressor_run(agent, ctx=addressor_ctx()) as run:
            await _address(merge_request=merge_request, thread_id=session.thread_id, sandbox_env_id="env-1")

        run.build_spec.assert_awaited_once_with("env-1")
        assert run.context_kwargs["ref"] == "feature"
        assert run.context_kwargs["merge_request"] is merge_request
        assert run.context_kwargs["fallback_ref_on_missing"] is False
        assert (await Session.objects.aget(thread_id=session.thread_id)).ref == "feature"
        run.persist.assert_not_awaited()
        assert run.claimed == []
        run.recover.assert_not_awaited()
        [reply] = mention.create_merge_request_comment.call_args_list
        assert reply.args[2] == "done"

    async def test_the_reply_carries_the_protected_branch_footer(self, mention):
        agent = addressor_agent(
            return_value={"messages": [AIMessage(content="done")]},
            state_values={"protected_branch_fallback_source": "feature", "merge_request": _merge_request(200)},
        )

        with addressor_run(agent, ctx=addressor_ctx()):
            await _address()

        [reply] = mention.create_merge_request_comment.call_args_list
        assert reply.args[2].startswith("done\n\n")
        assert "(!200)" in reply.args[2]

    async def test_the_reply_is_posted_repo_relative(self, mention):
        agent = addressor_agent(
            return_value={"messages": [AIMessage(content="Updated /workspace/repo/daiv/x.py.")]},
            state_values={"protected_branch_fallback_source": "feature", "merge_request": _merge_request(200)},
        )

        with addressor_run(agent, ctx=addressor_ctx()):
            await _address()

        [reply] = mention.create_merge_request_comment.call_args_list
        assert reply.args[2].startswith("Updated daiv/x.py.")
        assert "/workspace/repo/" not in reply.args[2]

    async def test_a_question_is_posted_under_the_mention_with_the_reply_footer(self, mention):
        messages = [HumanMessage(content="migrate"), *ask_user_question_messages()]
        agent = addressor_agent(return_value={"messages": messages}, state_values={"messages": messages})

        with addressor_run(agent, ctx=addressor_ctx()):
            await _address()

        [reply] = mention.create_merge_request_comment.call_args_list
        assert reply.args[2] == (
            f"{render_questions(SAMPLE_QUESTION_PAYLOAD)}\n\n@bob, reply mentioning @daiv-bot with your answer."
        )
        assert reply.kwargs["reply_to"] == CommentThread("c-1")

    async def test_a_mention_run_answers_the_note_it_was_started_for(self, captured_client):
        captured_client.get_merge_request_note.return_value = _note(12, "@daiv-bot add it", author="carol")
        agent = addressor_agent(return_value={"messages": [AIMessage(content="done")]})

        with addressor_run(agent, ctx=addressor_ctx()):
            await _address(mention_note_id=12)

        captured_client.get_merge_request_note.assert_called_once_with("owner/repo", 99, 12, on_diff=False)
        [prompt_message] = agent.ainvoke.await_args.args[0]["messages"]
        assert (prompt_message.name, prompt_message.content) == ("carol", "@daiv-bot add it")

    @pytest.mark.parametrize(
        ("location", "where"),
        [
            (DiffLocation(path="CHANGELOG.md"), "the file `CHANGELOG.md`"),
            (DiffLocation(path="app/x.py", line=12), "line 12 of `app/x.py`"),
            (DiffLocation(path="app/x.py", line=3, old_line=True), "line 3 (before the change) of `app/x.py`"),
        ],
        ids=["file", "line", "removed-line"],
    )
    async def test_a_diff_mention_tells_the_agent_where_it_was_left(self, captured_client, location, where):
        captured_client.get_merge_request_note.return_value = _note(12, "@daiv-bot shorten it")
        agent = addressor_agent(return_value={"messages": [AIMessage(content="done")]})

        with addressor_run(agent, ctx=addressor_ctx()):
            await _address(mention_note_id=12, mention_on_diff=True, mention_location=location)

        captured_client.get_merge_request_note.assert_called_once_with("owner/repo", 99, 12, on_diff=True)
        [prompt_message] = agent.ainvoke.await_args.args[0]["messages"]
        assert prompt_message.content == f"Comment on {where} in the merge request's diff:\n\n@daiv-bot shorten it"

    @pytest.mark.parametrize("platform", [GitPlatform.GITLAB, GitPlatform.GITHUB])
    async def test_a_diff_mention_is_answered_in_its_thread(self, mention, platform):
        mention.git_platform = platform
        agent = addressor_agent(return_value={"messages": [AIMessage(content="done")]})

        with addressor_run(agent, ctx=addressor_ctx()):
            await _address(mention_on_diff=True)

        [reply] = mention.create_merge_request_comment.call_args_list
        assert (reply.args[2], reply.kwargs["reply_to"]) == ("done", CommentThread("c-1", on_diff=True))

    async def test_a_failed_diff_mention_on_github_says_so_in_its_thread(self, mention):
        mention.git_platform = GitPlatform.GITHUB
        agent = addressor_agent(side_effect=RuntimeError("boom"))

        with addressor_run(agent, ctx=addressor_ctx()), pytest.raises(RuntimeError, match="boom"):
            await _address(mention_on_diff=True)

        [note] = mention.create_merge_request_comment.call_args_list
        assert "An unexpected error occurred while working on this pull request." in note.args[2]
        assert note.kwargs["reply_to"] == CommentThread("c-1", on_diff=True)

    async def test_a_job_runs_its_prompt_as_the_requesters_message(self, captured_client):
        agent = addressor_agent(return_value={"messages": [AIMessage(content="done")]})

        with addressor_run(agent, ctx=addressor_ctx()) as run:
            await _address(
                mention_comment_id=None,
                mention_note_id=None,
                prompt="/code-review",
                requester_username="pusher",
                agent_model="openrouter:x",
            )

        captured_client.get_merge_request_note.assert_not_called()
        [prompt_message] = agent.ainvoke.await_args.args[0]["messages"]
        assert (prompt_message.name, prompt_message.content) == ("pusher", "/code-review")
        assert run.context_kwargs["merge_request"].merge_request_id == 99
        [reply] = captured_client.create_merge_request_comment.call_args_list
        assert (reply.args[2], reply.kwargs["reply_to"]) == ("done", None)

    async def test_a_jobs_question_is_a_new_comment_on_the_merge_request(self, captured_client):
        messages = [HumanMessage(content="migrate"), *ask_user_question_messages()]
        agent = addressor_agent(return_value={"messages": messages}, state_values={"messages": messages})

        with addressor_run(agent, ctx=addressor_ctx()):
            await _address(mention_comment_id=None, mention_note_id=None, prompt="migrate", requester_username="ci")

        [reply] = captured_client.create_merge_request_comment.call_args_list
        assert reply.args[2].endswith("@ci, reply mentioning @daiv-bot with your answer.")
        assert reply.kwargs["reply_to"] is None

    async def test_a_failed_job_posts_the_unable_note_as_a_new_comment(self, captured_client):
        agent = addressor_agent(side_effect=RuntimeError("boom"))

        with addressor_run(agent, ctx=addressor_ctx()), pytest.raises(RuntimeError, match="boom"):
            await _address(mention_comment_id=None, mention_note_id=None, prompt="p", requester_username="ci")

        [note] = captured_client.create_merge_request_comment.call_args_list
        assert _UNABLE in note.args[2]
        assert note.kwargs["reply_to"] is None

    async def test_a_mention_without_a_note_id_says_it_is_unable_and_raises(self, mention):
        with (
            addressor_run(addressor_agent(), ctx=addressor_ctx()) as run,
            pytest.raises(ValueError, match="mention_note_id"),
        ):
            await _address(mention_note_id=None)

        run.create_agent.assert_not_awaited()
        [note] = mention.create_merge_request_comment.call_args_list
        assert _UNABLE in note.args[2]

    async def test_a_question_on_github_is_not_threaded(self, mention):
        mention.git_platform = GitPlatform.GITHUB
        mention.get_merge_request_note.return_value = _note(7, "please fix")
        messages = [HumanMessage(content="migrate"), *ask_user_question_messages()]
        agent = addressor_agent(return_value={"messages": messages}, state_values={"messages": messages})

        with addressor_run(agent, ctx=addressor_ctx()):
            await _address(mention_comment_id="7", mention_note_id=7)

        [reply] = mention.create_merge_request_comment.call_args_list
        assert reply.kwargs["reply_to"] is None

    @pytest.mark.django_db(transaction=True)
    async def test_an_agent_error_recovers_a_draft_says_so_and_re_raises(self, mention):
        session = await _review_session(str(uuid.uuid4()))
        agent = addressor_agent(
            side_effect=RuntimeError("boom"),
            state_values={"merge_request": _merge_request(source_branch="daiv/published"), "published": True},
        )

        with (
            addressor_run(agent, ctx=addressor_ctx(), draft_published=True) as run,
            pytest.raises(RuntimeError, match="boom"),
        ):
            await _address(thread_id=session.thread_id)

        assert run.recover.await_args.kwargs == {
            "thread_id": session.thread_id,
            "workspace": run.create_agent.await_args.kwargs["workspace"],
            "settings": run.resolve.return_value,
            "session_links": run.create_agent.await_args.kwargs["session_links"],
            "on_publish": ANY,
        }
        [note] = mention.create_merge_request_comment.call_args_list
        assert "committed the changes done so far" in note.args[2]
        assert note.kwargs["reply_to"] == CommentThread("c-1")
        assert (await Session.objects.aget(thread_id=session.thread_id)).ref == "feature"
        assert run.claimed == []

    async def test_an_agent_error_note_carries_the_footer_from_the_recovered_checkpoint(self, mention):
        """The footer comes from the checkpoint re-read after recovery, which may have swapped to a fresh MR."""
        agent = addressor_agent(
            side_effect=RuntimeError("boom"),
            state_values={"merge_request": _merge_request(source_branch="daiv/published")},
        )
        recovered_state = SimpleNamespace(
            values={"protected_branch_fallback_source": "feature", "merge_request": _merge_request(200)}
        )

        async def _recover(*_args, **_kwargs):
            agent.aget_state.return_value = recovered_state
            return SimpleNamespace(created_merge_request=False)

        with (
            addressor_run(agent, ctx=addressor_ctx(), stub_recovery=False),
            patch("sessions.executor.run.recover_draft", new=AsyncMock(side_effect=_recover)),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await _address()

        [note] = mention.create_merge_request_comment.call_args_list
        assert "committed the changes done so far" in note.args[2]
        assert "(!200)" in note.args[2]

    @pytest.mark.parametrize("error_cls", [AgentConfigurationError, CloneCredentialUnavailableError])
    async def test_a_configuration_refusal_says_it_cannot_run_yet_and_re_raises(self, mention, error_cls):
        with (
            addressor_run(addressor_agent(), ctx=addressor_ctx(), kwargs_error=error_cls("no model")),
            pytest.raises(error_cls),
        ):
            await _address()

        [note] = mention.create_merge_request_comment.call_args_list
        assert note.args[2] == "@alice I can't run yet: no model"

    async def test_a_failed_clone_says_so_on_the_merge_request(self, mention):
        """B14: a failure before the agent starts posts the unable note instead of leaving only the 👀 reaction."""
        with (
            addressor_run(addressor_agent(), context=clone_raising(OSError("clone failed"))) as run,
            pytest.raises(OSError, match="clone failed"),
        ):
            await _address()

        run.create_agent.assert_not_awaited()
        [note] = mention.create_merge_request_comment.call_args_list
        assert _UNABLE in note.args[2]

    async def test_a_vanished_source_branch_is_left_to_the_task(self, mention):
        """The task answers a deleted source branch with its own comment, so the addressor posts nothing."""
        with (
            addressor_run(addressor_agent(), context=clone_raising(CloneRefNotFoundError("feature", "owner/repo"))),
            pytest.raises(CloneRefNotFoundError),
        ):
            await _address()

        mention.create_merge_request_comment.assert_not_called()

    @pytest.mark.django_db(transaction=True)
    async def test_a_session_slot_that_never_frees_says_so_on_the_merge_request(self, mention):
        """B14: a run that gave up waiting for the slot tells the merge request instead of going quiet."""
        thread_id = str(uuid.uuid4())
        await _review_session(thread_id, active_run_id="chat-run")

        with (
            addressor_run(addressor_agent(), real_lock=True) as run,
            patch("sessions.executor.lock.LOCK_WAIT_TIMEOUT_S", 0.05),
            patch("sessions.executor.lock.LOCK_POLL_INTERVAL_S", 0.01),
            pytest.raises(SessionLockTimeoutError),
        ):
            await _address(thread_id=thread_id)

        run.create_agent.assert_not_awaited()
        [note] = mention.create_merge_request_comment.call_args_list
        assert _UNABLE in note.args[2]


@pytest.mark.django_db(transaction=True)
async def test_a_webhook_run_records_the_model_it_ran_on_its_run_row(mention):
    thread_id = str(uuid.uuid4())
    session = await _review_session(thread_id)
    run = await Run.objects.acreate(session=session, trigger_type=SessionOrigin.MR_WEBHOOK, repo_id="owner/repo")

    with addressor_run(addressor_agent(return_value={"messages": [AIMessage(content="done")]}), ctx=addressor_ctx()):
        await _address(thread_id=thread_id, run_id=str(run.pk))

    await run.arefresh_from_db()
    assert (run.agent_model, run.agent_thinking_level) == ("m", "medium")


@pytest.mark.django_db(transaction=True)
async def test_a_daiv_max_comment_runs_on_the_max_model_it_stamped(mention):
    site = {
        "agent_model_name": "site-default",
        "agent_thinking_level": "low",
        "agent_max_model_name": "site-max",
        "agent_max_thinking_level": "high",
    }
    thread_id = str(uuid.uuid4())
    agent = addressor_agent(return_value={"messages": [AIMessage(content="done")]})

    with patch.multiple(site_settings, **site):
        run = await acreate_run(
            trigger_type=SessionOrigin.MR_WEBHOOK,
            task_result_id=None,
            repo_id="owner/repo",
            ref="feature",
            merge_request_iid=99,
            agent_model="site-max",
            agent_thinking_level="high",
            thread_id=thread_id,
        )
        with addressor_run(
            agent, ctx=addressor_ctx(), resolve=resolve_agent_settings, site=site_snapshot(**site)
        ) as stack:
            await _address(thread_id=thread_id, run_id=str(run.pk), use_max=True)

    await run.arefresh_from_db()
    assert stack.create_agent.await_args.kwargs["settings"].agent.names[0] == "site-max"
    assert (run.agent_model, run.agent_thinking_level) == ("site-max", "high")


def _new_mr_value(merge_request_id: int = 200) -> MergeRequest:
    return MergeRequest(
        repo_id="owner/repo",
        merge_request_id=merge_request_id,
        source_branch="agent/fresh-branch",
        target_branch="main",
        title="agent",
        description="d",
        author=_AUTHOR,
        web_url="https://gitlab.example.com/owner/repo/-/merge_requests/200",
    )


def _make_manager(*, platform: GitPlatform = GitPlatform.GITLAB) -> CommentsAddressorManager:
    manager = CommentsAddressorManager(
        repo_id="owner/repo", merge_request=_merge_request(), mention_comment_id="c1", thread_id="t-1"
    )
    manager.client.git_platform = platform
    return manager


class TestRenderProtectedBranchFooter:
    def test_renders_when_both_keys_present(self, stub_base_init):
        manager = _make_manager()
        snapshot = SimpleNamespace(
            values={"protected_branch_fallback_source": "feature", "merge_request": _new_mr_value()}
        )

        rendered = manager._render_protected_branch_footer(snapshot)

        assert rendered is not None
        assert "feature" in rendered
        assert "https://gitlab.example.com/owner/repo/-/merge_requests/200" in rendered
        assert "!200" in rendered  # GitLab-style ref

    def test_renders_github_style_for_github_platform(self, stub_base_init):
        manager = _make_manager(platform=GitPlatform.GITHUB)
        snapshot = SimpleNamespace(
            values={"protected_branch_fallback_source": "feature", "merge_request": _new_mr_value()}
        )

        rendered = manager._render_protected_branch_footer(snapshot)

        assert rendered is not None
        assert "#200" in rendered  # GitHub-style ref
        assert "pull request" in rendered  # GitHub vocabulary

    def test_returns_none_when_snapshot_is_none(self, stub_base_init):
        """A failed checkpoint read upstream surfaces as ``None``; render must short-circuit cleanly."""
        manager = _make_manager()
        assert manager._render_protected_branch_footer(None) is None

    def test_returns_none_silently_when_source_branch_missing(self, stub_base_init):
        """No fallback happened: ``merge_request`` set with no fallback source is the ordinary
        MR-scope state, not a partial checkpoint, so render must stay silent (no warning)."""
        manager = _make_manager()
        snapshot = SimpleNamespace(values={"merge_request": _new_mr_value()})
        with patch("sessions.merge_request_runs.addressor.logger") as mock_logger:
            assert manager._render_protected_branch_footer(snapshot) is None
        mock_logger.warning.assert_not_called()

    def test_warns_when_merge_request_missing_but_source_present(self, stub_base_init):
        """A fallback source with no MR is genuinely partial/raced; surface it to the operator."""
        manager = _make_manager()
        snapshot = SimpleNamespace(values={"protected_branch_fallback_source": "feature"})
        with patch("sessions.merge_request_runs.addressor.logger") as mock_logger:
            assert manager._render_protected_branch_footer(snapshot) is None
        mock_logger.warning.assert_called_once()

    def test_returns_none_silently_when_source_branch_empty_string(self, stub_base_init):
        """An empty source-branch is the no-fallback signal; no footer and no warning."""
        manager = _make_manager()
        snapshot = SimpleNamespace(values={"protected_branch_fallback_source": "", "merge_request": _new_mr_value()})
        with patch("sessions.merge_request_runs.addressor.logger") as mock_logger:
            assert manager._render_protected_branch_footer(snapshot) is None
        mock_logger.warning.assert_not_called()


class TestAppendFooter:
    def test_returns_body_unchanged_when_footer_is_none(self):
        assert CommentsAddressorManager._append_footer("the body", None) == "the body"

    def test_returns_body_unchanged_when_footer_is_empty(self):
        assert CommentsAddressorManager._append_footer("the body", "") == "the body"

    def test_separates_body_and_footer_with_blank_line(self):
        result = CommentsAddressorManager._append_footer("the body", "the footer")
        assert result == "the body\n\nthe footer"

    def test_strips_trailing_whitespace_from_body(self):
        """Stops a trailing newline from compounding into a triple-newline that
        breaks the GitLab/GitHub markdown renderer."""
        result = CommentsAddressorManager._append_footer("body\n\n\n", "footer")
        assert result == "body\n\nfooter"

    def test_strips_leading_whitespace_from_footer(self):
        result = CommentsAddressorManager._append_footer("body", "\n\nfooter")
        assert result == "body\n\nfooter"

    def test_handles_empty_body(self):
        """An empty body + footer must not produce a leading blank line that some
        markdown renderers swallow as front-matter."""
        result = CommentsAddressorManager._append_footer("", "footer")
        assert result == "\n\nfooter"


class TestProviderFailureNote:
    @patch("core.models.Provider.display_name_for", return_value="OpenRouter")
    @pytest.mark.parametrize("platform", [GitPlatform.GITLAB, GitPlatform.GITHUB])
    async def test_a_provider_failure_says_so_on_the_merge_request(self, _display_name, stub_base_init, platform):
        manager = _make_manager(platform=platform)
        manager.client.current_user = User(id=2, username="daiv")
        error = RuntimeError("402 from provider")
        tag_provider_failure(error, ProviderFailure("out_of_credits", "openrouter"))

        with patch.object(manager, "_leave_comment") as leave_comment:
            await manager._on_failure(error, draft_published=True, snapshot=None)

        [(body,), _] = leave_comment.call_args
        assert "OpenRouter is out of credits, so DAIV couldn't" in body
        assert "To have" in body
        assert "committed the changes done so far" in body
        assert "unexpected error" not in body

    async def test_an_unclassified_failure_keeps_the_generic_note(self, stub_base_init):
        manager = _make_manager()
        manager.client.current_user = User(id=2, username="daiv")

        with patch.object(manager, "_leave_comment") as leave_comment:
            await manager._on_failure(RuntimeError("boom"), draft_published=False, snapshot=None)

        [(body,), _] = leave_comment.call_args
        assert "An unexpected error occurred while working on this merge request." in body
