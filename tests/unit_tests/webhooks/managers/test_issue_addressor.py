from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage
from sessions.executor.lock import SessionLockTimeoutError
from sessions.locks import SessionLock
from sessions.models import Session, SessionOrigin
from webhooks.managers.issue_addressor import ADDRESS_ISSUE_PROMPT, PLAN_ISSUE_PROMPT, IssueAddressorManager

from automation.agent.utils import get_daiv_agent_kwargs
from automation.agent.validators import AgentConfigurationError
from codebase.base import Issue, MergeRequest, User
from codebase.repo_config import RepositoryConfig
from core.constants import BOT_AUTO_LABEL, BOT_LABEL
from core.sandbox.schemas import StartSessionRequest
from core.site_settings import site_settings
from tests.unit_tests.conftest import FakeSandboxClient, bound_run_sandbox_client, sandbox_spec
from tests.unit_tests.sessions.conftest import active_holder
from tests.unit_tests.sessions.executor.conftest import publisher_through_backend
from tests.unit_tests.webhooks.managers.conftest import addressor_agent, addressor_run, clone_raising

_AUTHOR = User(id=1, username="alice")
_UNABLE = "An unexpected error occurred while working on this issue."


def _ctx() -> SimpleNamespace:
    """The ``RuntimeCtx`` the stubbed clone yields: only what the executor reads."""
    return SimpleNamespace(config=RepositoryConfig(), repo=SimpleNamespace(ref="main"))


def _sandbox_ctx() -> SimpleNamespace:
    """``_ctx()`` for a sandbox run, with what draft recovery reads."""
    return SimpleNamespace(**vars(_ctx()), merge_request=None, gitrepo=None, sandbox=sandbox_spec())


def _issue(*, labels: list[str]) -> Issue:
    return Issue(id=1, iid=42, title="t", author=_AUTHOR, labels=labels)


def _merge_request() -> MergeRequest:
    return MergeRequest(
        repo_id="owner/repo",
        merge_request_id=7,
        source_branch="daiv/issue-42",
        target_branch="main",
        title="t",
        description="d",
        author=_AUTHOR,
        draft=True,
    )


async def _issue_session(**fields) -> str:
    thread_id = str(uuid.uuid4())
    await Session.objects.acreate(
        thread_id=thread_id, origin=SessionOrigin.ISSUE_WEBHOOK, repo_id="owner/repo", **fields
    )
    return thread_id


async def _address(**kwargs):
    """``address_issue`` for a label-triggered issue on ``owner/repo``; ``kwargs`` override."""
    return await IssueAddressorManager.address_issue(
        **({"repo_id": "owner/repo", "issue": _issue(labels=[BOT_LABEL])} | kwargs)
    )


class TestMaxLabelRoutesToMaxModel:
    """Lock the webhook → ``use_max`` → ``site_settings.agent_max_*`` contract, checked at the ``create_daiv_agent``
    boundary with the real model resolution."""

    @staticmethod
    async def _agent_kwargs(labels: list[str]) -> dict:
        agent = addressor_agent(return_value={"messages": [AIMessage(content="done")]})
        with addressor_run(agent, ctx=_ctx(), resolve=get_daiv_agent_kwargs) as run:
            await _address(issue=_issue(labels=labels))
        return run.create_agent.await_args.kwargs

    async def test_max_label_resolves_to_max_model(self, stub_base_init):
        """``daiv-max`` label → primary model is ``site_settings.agent_max_model_name`` and thinking level is
        ``site_settings.agent_max_thinking_level``. The repo-config model is preserved as a fallback so the run
        degrades cleanly on provider outage."""
        captured = await self._agent_kwargs(["daiv-max"])

        model_names = captured["model_names"]
        assert model_names[0] == site_settings.agent_max_model_name
        assert captured["thinking_level"] == site_settings.agent_max_thinking_level
        assert RepositoryConfig().models.agent.model in model_names[1:]

    async def test_max_label_case_insensitive(self, stub_base_init):
        """Label matching must be case-insensitive — GitHub UIs upper-case labels freely."""
        captured = await self._agent_kwargs(["DAIV-MAX"])

        assert captured["model_names"][0] == site_settings.agent_max_model_name
        assert captured["thinking_level"] == site_settings.agent_max_thinking_level

    async def test_no_max_label_uses_repo_config_model(self, stub_base_init):
        """Without ``daiv-max`` the resolved primary model comes from the repo's ``AgentModelConfig``, proving the
        ``use_max`` branch is the only path to the max model."""
        captured = await self._agent_kwargs(["daiv"])
        repo_agent_cfg = RepositoryConfig().models.agent

        assert captured["model_names"][0] == repo_agent_cfg.model
        assert captured["thinking_level"] == repo_agent_cfg.thinking_level
        assert site_settings.agent_max_model_name not in captured["model_names"]


class TestIssueAfterRunMatrix:
    @pytest.mark.django_db(transaction=True)
    async def test_it_waits_while_another_holder_has_the_session_slot(self, captured_client):
        """B13: the run waits for the slot, then runs holding it."""
        thread_id = await _issue_session(active_run_id="chat-run")
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
            addressor_run(addressor_agent(side_effect=_invoke), real_lock=True),
            patch("sessions.executor.lock.LOCK_POLL_INTERVAL_S", 0.01),
            patch("sessions.executor.lock.SessionLock.try_claim", _try_claim),
        ):
            await _address(thread_id=thread_id)

        [holder] = holders
        assert holder.startswith("webhook-")
        assert await active_holder(thread_id) is None

    async def test_a_successful_run_persists_the_ref_and_arms_the_watch(self, captured_client):
        mr = _merge_request()
        agent = addressor_agent(
            return_value={"messages": [AIMessage(content="done")]},
            state_values={"merge_request": mr, "published": True},
        )
        issue = _issue(labels=[BOT_LABEL])

        with addressor_run(agent, ctx=_ctx()) as run:
            await _address(issue=issue, ref="fix/42", thread_id="t-issue")

        assert run.context_kwargs["fallback_ref_on_missing"] is True
        assert run.context_kwargs["issue"] is issue
        assert run.context_kwargs["ref"] == "fix/42"
        run.persist.assert_awaited_once_with(thread_id="t-issue", current_ref="main", merge_request=mr)
        assert run.armed == [
            {"repo_id": "owner/repo", "run_id": None, "merge_request": mr, "published": True, "user_id": None}
        ]
        run.recover.assert_not_awaited()
        [reply] = captured_client.create_issue_comment.call_args_list
        assert reply.args[2] == "done"

    async def test_an_agent_error_recovers_a_draft_says_so_and_re_raises(self, captured_client):
        with (
            addressor_run(addressor_agent(side_effect=RuntimeError("boom")), draft_published=True) as run,
            pytest.raises(RuntimeError, match="boom"),
        ):
            await _address(thread_id="t-issue")

        assert run.recover.await_args.kwargs == {"thread_id": "t-issue"}
        [note] = captured_client.create_issue_comment.call_args_list
        assert "To avoid losing progress" in note.args[2]
        run.persist.assert_not_awaited()
        assert run.armed == []

    async def test_an_agent_error_recovers_the_draft_through_the_live_session(self, captured_client):
        """B7: after an agent error, the draft is pushed through the open run client and the turn's own session."""
        client = FakeSandboxClient.opened()
        started: list[str] = []

        async def _fail_mid_turn(*_args, **_kwargs):
            started.append(await client.start_session(StartSessionRequest(base_image="python:3.12")))
            raise RuntimeError("boom")

        agent = addressor_agent(side_effect=_fail_mid_turn)
        agent.aget_state = AsyncMock(
            side_effect=lambda **_kwargs: SimpleNamespace(values={"merge_request": None, "session_id": started[0]})
        )
        agent.aupdate_state = AsyncMock()
        created: list = []
        with (
            bound_run_sandbox_client(client),
            addressor_run(agent, ctx=_sandbox_ctx(), stub_recovery=False),
            patch(
                "automation.agent.publishers.GitChangePublisher",
                publisher_through_backend(created, publishes=_merge_request()),
            ),
            patch("codebase.utils.get_repo_ref", return_value="daiv/issue-42"),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await _address()

        assert client.calls_to("run_commands") == [(started[0], ("git push origin HEAD",))]
        [note] = captured_client.create_issue_comment.call_args_list
        assert "To avoid losing progress" in note.args[2]

    async def test_a_missing_model_says_it_cannot_run_yet_and_returns(self, captured_client):
        captured_client.get_issue_comment.return_value = SimpleNamespace(
            notes=[SimpleNamespace(author=SimpleNamespace(username="bob"), id="n1", body="please")]
        )
        with addressor_run(addressor_agent(), kwargs_error=AgentConfigurationError("no model")) as run:
            result = await _address(mention_comment_id="c-1")

        assert result is None
        run.create_agent.assert_not_awaited()
        [note] = captured_client.create_issue_comment.call_args_list
        assert note.args[2] == "@alice I can't run yet: no model"
        assert note.kwargs["reply_to_id"] == "c-1"

    async def test_a_failed_clone_says_so_on_the_issue(self, captured_client):
        """B14: a failure before the agent starts posts the unable note instead of leaving only the 👀 reaction."""
        with (
            addressor_run(addressor_agent(), context=clone_raising(OSError("clone failed"))) as run,
            pytest.raises(OSError, match="clone failed"),
        ):
            await _address()

        run.create_agent.assert_not_awaited()
        run.recover.assert_not_awaited()
        [note] = captured_client.create_issue_comment.call_args_list
        assert _UNABLE in note.args[2]
        assert "To avoid losing progress" not in note.args[2]

    @pytest.mark.django_db(transaction=True)
    async def test_a_session_slot_that_never_frees_says_so_on_the_issue(self, captured_client):
        """B14: a run that gave up waiting for the slot tells the issue instead of going quiet."""
        thread_id = await _issue_session(active_run_id="chat-run")

        with (
            addressor_run(addressor_agent(), real_lock=True) as run,
            patch("webhooks.managers.base.LOCK_WAIT_TIMEOUT_S", 0.05),
            patch("sessions.executor.lock.LOCK_POLL_INTERVAL_S", 0.01),
            pytest.raises(SessionLockTimeoutError),
        ):
            await _address(thread_id=thread_id)

        run.create_agent.assert_not_awaited()
        [note] = captured_client.create_issue_comment.call_args_list
        assert _UNABLE in note.args[2]
        assert await active_holder(thread_id) == "chat-run"

    @pytest.mark.parametrize(
        ("label", "prompt"), [(BOT_AUTO_LABEL, ADDRESS_ISSUE_PROMPT), (BOT_LABEL, PLAN_ISSUE_PROMPT)]
    )
    async def test_the_label_picks_the_prompt(self, captured_client, label: str, prompt: str):
        agent = addressor_agent(return_value={"messages": [AIMessage(content="done")]})

        with addressor_run(agent):
            await _address(issue=_issue(labels=[label]))

        [message] = agent.ainvoke.await_args.args[0]["messages"]
        assert message.content == prompt.format(issue_iid=42)
