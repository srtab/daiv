from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from sessions.executor.lock import SessionLockTimeoutError
from sessions.locks import SessionLock
from sessions.models import Session, SessionOrigin
from webhooks.managers.review_addressor import CommentsAddressorManager

from automation.agent.validators import AgentConfigurationError
from codebase.base import MergeRequest, User
from codebase.exceptions import CloneRefNotFoundError
from tests.unit_tests.sessions.conftest import active_holder
from tests.unit_tests.webhooks.managers.conftest import addressor_agent, addressor_run, clone_raising

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


def _ctx() -> SimpleNamespace:
    """The ``RuntimeCtx`` the stubbed clone yields: only what the executor reads."""
    return SimpleNamespace(config=MagicMock(), repo=SimpleNamespace(ref="feature"))


@pytest.fixture
def mention(captured_client):
    captured_client.get_merge_request_comment.return_value = SimpleNamespace(
        notes=[SimpleNamespace(author=SimpleNamespace(username="bob"), id="n1", body="please fix")]
    )
    return captured_client


async def _review_session(thread_id: str, **fields) -> Session:
    return await Session.objects.acreate(
        thread_id=thread_id, origin=SessionOrigin.MR_WEBHOOK, repo_id="owner/repo", ref="feature", **fields
    )


async def _address(**kwargs):
    """``address_comments`` for mention ``c-1`` on MR 99 of ``owner/repo``; ``kwargs`` override."""
    return await CommentsAddressorManager.address_comments(
        **({"repo_id": "owner/repo", "merge_request": _merge_request(), "mention_comment_id": "c-1"} | kwargs)
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
            addressor_run(addressor_agent(side_effect=_invoke), real_lock=True, ctx=_ctx()),
            patch("sessions.executor.lock.LOCK_POLL_INTERVAL_S", 0.01),
            patch("sessions.executor.lock.SessionLock.try_claim", _try_claim),
        ):
            await _address(thread_id=thread_id)

        [holder] = holders
        assert holder.startswith("webhook-")
        assert await active_holder(thread_id) is None

    @pytest.mark.django_db(transaction=True)
    async def test_a_successful_run_neither_moves_the_ref_nor_arms_the_watch(self, mention):
        """The checkpoint names another published branch, so any ref sync would show on the session row."""
        session = await _review_session(str(uuid.uuid4()))
        agent = addressor_agent(
            return_value={"messages": [AIMessage(content="done")]},
            state_values={"merge_request": _merge_request(source_branch="daiv/published"), "published": True},
        )
        merge_request = _merge_request()

        with addressor_run(agent, ctx=_ctx()) as run:
            await _address(merge_request=merge_request, thread_id=session.thread_id)

        assert run.context_kwargs["ref"] == "feature"
        assert run.context_kwargs["merge_request"] is merge_request
        assert run.context_kwargs["fallback_ref_on_missing"] is False
        assert (await Session.objects.aget(thread_id=session.thread_id)).ref == "feature"
        run.persist.assert_not_awaited()
        assert run.armed == []
        run.recover.assert_not_awaited()
        [reply] = mention.create_merge_request_comment.call_args_list
        assert reply.args[2] == "done"

    async def test_the_reply_carries_the_protected_branch_footer(self, mention):
        agent = addressor_agent(
            return_value={"messages": [AIMessage(content="done")]},
            state_values={"protected_branch_fallback_source": "feature", "merge_request": _merge_request(200)},
        )

        with addressor_run(agent, ctx=_ctx()):
            await _address()

        [reply] = mention.create_merge_request_comment.call_args_list
        assert reply.args[2].startswith("done\n\n")
        assert "(!200)" in reply.args[2]

    @pytest.mark.django_db(transaction=True)
    async def test_an_agent_error_recovers_a_draft_says_so_and_re_raises(self, mention):
        session = await _review_session(str(uuid.uuid4()))
        agent = addressor_agent(
            side_effect=RuntimeError("boom"),
            state_values={"merge_request": _merge_request(source_branch="daiv/published"), "published": True},
        )

        with addressor_run(agent, ctx=_ctx(), draft_published=True) as run, pytest.raises(RuntimeError, match="boom"):
            await _address(thread_id=session.thread_id)

        assert run.recover.await_args.kwargs == {"thread_id": session.thread_id}
        [note] = mention.create_merge_request_comment.call_args_list
        assert "committed the changes done so far" in note.args[2]
        assert note.kwargs["reply_to_id"] == "c-1"
        assert (await Session.objects.aget(thread_id=session.thread_id)).ref == "feature"
        assert run.armed == []

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
            return True

        with (
            addressor_run(agent, ctx=_ctx(), stub_recovery=False),
            patch("sessions.executor.run.recover_draft", new=AsyncMock(side_effect=_recover)),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await _address()

        [note] = mention.create_merge_request_comment.call_args_list
        assert "committed the changes done so far" in note.args[2]
        assert "(!200)" in note.args[2]

    async def test_a_missing_model_says_it_cannot_run_yet_and_re_raises(self, mention):
        with (
            addressor_run(addressor_agent(), ctx=_ctx(), kwargs_error=AgentConfigurationError("no model")),
            pytest.raises(AgentConfigurationError),
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
            patch("webhooks.managers.base.LOCK_WAIT_TIMEOUT_S", 0.05),
            patch("sessions.executor.lock.LOCK_POLL_INTERVAL_S", 0.01),
            pytest.raises(SessionLockTimeoutError),
        ):
            await _address(thread_id=thread_id)

        run.create_agent.assert_not_awaited()
        [note] = mention.create_merge_request_comment.call_args_list
        assert _UNABLE in note.args[2]
