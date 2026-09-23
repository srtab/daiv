from __future__ import annotations

import uuid
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from sessions.models import Session, SessionOrigin
from sessions.pipeline_watch.service import PipelineWatch

from automation.agent.validators import AgentConfigurationError
from codebase.base import GitPlatform, MergeRequest, User
from codebase.managers.base import BaseManager
from codebase.managers.review_addressor import CommentsAddressorManager

_AUTHOR = User(id=1, username="alice")


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
    return SimpleNamespace(
        repository=SimpleNamespace(slug="owner/repo"),
        git_platform=GitPlatform.GITLAB,
        bot_username="daiv-bot",
        config=MagicMock(),
        acting_user_id=None,
        repo=SimpleNamespace(ref="feature"),
    )


@asynccontextmanager
async def _noop_checkpointer():
    yield MagicMock()


def _agent(*, state_values: dict | None = None, **ainvoke) -> MagicMock:
    agent = MagicMock()
    agent.get_name.return_value = "daiv"
    agent.ainvoke = AsyncMock(**ainvoke)
    agent.aget_state = AsyncMock(return_value=SimpleNamespace(values=state_values or {}))
    return agent


@contextmanager
def _review_run(agent: MagicMock, *, draft_published: bool = False, kwargs_error: Exception | None = None):
    """Stub everything around the agent; yield the watch and draft-recovery mocks."""
    run = SimpleNamespace(arm=AsyncMock())
    with (
        patch("codebase.managers.review_addressor.open_checkpointer", _noop_checkpointer),
        patch(
            "codebase.managers.review_addressor.get_daiv_agent_kwargs",
            return_value={"model_names": ["m"], "thinking_level": "medium"},
            side_effect=kwargs_error,
        ),
        patch("codebase.managers.review_addressor.create_daiv_agent", AsyncMock(return_value=agent)),
        patch("codebase.managers.review_addressor.build_langsmith_config", return_value={}),
        patch("codebase.managers.review_addressor.track_usage_metadata", MagicMock()),
        patch.object(PipelineWatch, "aarm_after_run", run.arm),
        patch.object(CommentsAddressorManager, "_recover_draft", AsyncMock(return_value=draft_published)) as recover,
        patch.object(BaseManager, "_build_agent_result", AsyncMock(return_value={})),
    ):
        run.recover = recover
        yield run


@pytest.fixture
def mention(captured_client):
    captured_client.get_merge_request_comment.return_value = SimpleNamespace(
        notes=[SimpleNamespace(author=SimpleNamespace(username="bob"), id="n1", body="please fix")]
    )
    return captured_client


async def _review_session(thread_id: str) -> Session:
    return await Session.objects.acreate(
        thread_id=thread_id, origin=SessionOrigin.MR_WEBHOOK, repo_id="owner/repo", ref="feature"
    )


class TestReviewAfterRunMatrix:
    @pytest.mark.django_db(transaction=True)
    async def test_it_runs_while_another_holder_has_the_session_slot(self, mention):
        """No session lock: the run proceeds while another holder has the slot."""
        thread_id = str(uuid.uuid4())
        await Session.objects.acreate(
            thread_id=thread_id, origin=SessionOrigin.MR_WEBHOOK, repo_id="owner/repo", active_run_id="chat-run"
        )
        agent = _agent(return_value={"messages": [AIMessage(content="done")]})

        with _review_run(agent):
            await CommentsAddressorManager.address_comments(
                merge_request=_merge_request(), mention_comment_id="c-1", runtime_ctx=_ctx(), thread_id=thread_id
            )

        agent.ainvoke.assert_awaited_once()
        assert (await Session.objects.aget(thread_id=thread_id)).active_run_id == "chat-run"

    @pytest.mark.django_db(transaction=True)
    async def test_a_successful_run_neither_moves_the_ref_nor_arms_the_watch(self, mention):
        """The checkpoint names another published branch, so any ref sync would show on the session row."""
        session = await _review_session(str(uuid.uuid4()))
        agent = _agent(
            return_value={"messages": [AIMessage(content="done")]},
            state_values={"merge_request": _merge_request(source_branch="daiv/published"), "published": True},
        )

        with _review_run(agent) as run:
            await CommentsAddressorManager.address_comments(
                merge_request=_merge_request(),
                mention_comment_id="c-1",
                runtime_ctx=_ctx(),
                thread_id=session.thread_id,
            )

        assert (await Session.objects.aget(thread_id=session.thread_id)).ref == "feature"
        run.arm.assert_not_awaited()
        run.recover.assert_not_awaited()
        [reply] = mention.create_merge_request_comment.call_args_list
        assert reply.args[2] == "done"

    async def test_the_reply_carries_the_protected_branch_footer(self, mention):
        agent = _agent(
            return_value={"messages": [AIMessage(content="done")]},
            state_values={"protected_branch_fallback_source": "feature", "merge_request": _merge_request(200)},
        )

        with _review_run(agent):
            await CommentsAddressorManager.address_comments(
                merge_request=_merge_request(), mention_comment_id="c-1", runtime_ctx=_ctx()
            )

        [reply] = mention.create_merge_request_comment.call_args_list
        assert reply.args[2].startswith("done\n\n")
        assert "(!200)" in reply.args[2]

    @pytest.mark.django_db(transaction=True)
    async def test_an_agent_error_recovers_a_draft_says_so_and_re_raises(self, mention):
        session = await _review_session(str(uuid.uuid4()))
        agent = _agent(
            side_effect=RuntimeError("boom"),
            state_values={"merge_request": _merge_request(source_branch="daiv/published"), "published": True},
        )

        with _review_run(agent, draft_published=True) as run, pytest.raises(RuntimeError, match="boom"):
            await CommentsAddressorManager.address_comments(
                merge_request=_merge_request(),
                mention_comment_id="c-1",
                runtime_ctx=_ctx(),
                thread_id=session.thread_id,
            )

        assert run.recover.await_args.kwargs == {"entity_label": "merge request", "entity_id": 99}
        [note] = mention.create_merge_request_comment.call_args_list
        assert "committed the changes done so far" in note.args[2]
        assert note.kwargs["reply_to_id"] == "c-1"
        assert (await Session.objects.aget(thread_id=session.thread_id)).ref == "feature"
        run.arm.assert_not_awaited()

    async def test_a_missing_model_says_it_cannot_run_yet_and_re_raises(self, mention):
        with (
            _review_run(_agent(), kwargs_error=AgentConfigurationError("no model")),
            pytest.raises(AgentConfigurationError),
        ):
            await CommentsAddressorManager.address_comments(
                merge_request=_merge_request(), mention_comment_id="c-1", runtime_ctx=_ctx()
            )

        [note] = mention.create_merge_request_comment.call_args_list
        assert note.args[2] == "@alice I can't run yet: no model"
