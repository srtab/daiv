from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage
from sessions.models import Session, SessionOrigin

from automation.agent.validators import AgentConfigurationError
from codebase.base import GitPlatform, Issue, MergeRequest, User
from codebase.managers.issue_addressor import ADDRESS_ISSUE_PROMPT, PLAN_ISSUE_PROMPT, IssueAddressorManager
from codebase.repo_config import RepositoryConfig
from core.constants import BOT_AUTO_LABEL, BOT_LABEL
from core.sandbox.schemas import StartSessionRequest
from core.site_settings import site_settings
from tests.unit_tests.codebase.managers.conftest import (
    addressor_agent,
    addressor_run,
    open_noop_checkpointer,
    publisher_through_backend,
)
from tests.unit_tests.conftest import FakeSandboxClient, bound_run_sandbox_client, sandbox_runtime
from tests.unit_tests.sessions.conftest import watch_recorder

_AUTHOR = User(id=1, username="alice")


class _StubRepo:
    slug = "owner/repo"


def _ctx() -> SimpleNamespace:
    """Minimal RuntimeCtx stub: only the attributes ``_address_issue`` actually touches."""
    return SimpleNamespace(
        repository=_StubRepo(),
        git_platform=GitPlatform.GITLAB,
        bot_username="daiv-bot",
        config=RepositoryConfig(),
        acting_user_id=None,
        repo=SimpleNamespace(ref="main"),
    )


def _sandbox_ctx() -> SimpleNamespace:
    """``_ctx()`` for a sandbox run, with what draft recovery reads."""
    return SimpleNamespace(**vars(_ctx()), merge_request=None, gitrepo=None, sandbox=sandbox_runtime())


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


class _CapturedError(RuntimeError):
    """Sentinel raised by the patched ``create_daiv_agent`` to short-circuit ``_address_issue``
    immediately after kwargs resolution, without exercising the full agent invocation path."""


async def _run_addressor(*, labels: list[str]) -> dict:
    """Drive ``_address_issue`` far enough to capture the resolved ``create_daiv_agent`` kwargs.

    Patches the checkpointer context manager and ``create_daiv_agent`` (the boundary we care
    about), plus ``_add_unable_to_address_issue_note`` so the error-recovery path doesn't try
    to render a template or hit the stub client. Returns the kwargs that ``create_daiv_agent``
    was called with.
    """
    captured: dict = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        raise _CapturedError

    with (
        patch("codebase.managers.issue_addressor.open_checkpointer", open_noop_checkpointer),
        patch("codebase.managers.issue_addressor.create_daiv_agent", side_effect=_capture),
        patch.object(IssueAddressorManager, "_add_unable_to_address_issue_note"),
        pytest.raises(_CapturedError),
    ):
        await IssueAddressorManager.address_issue(issue=_issue(labels=labels), runtime_ctx=_ctx())

    return captured


class TestMaxLabelRoutesToMaxModel:
    """Lock the webhook → ``use_max`` → ``site_settings.agent_max_*`` contract.

    Webhook handlers bypass ``run_job_task`` and call ``create_daiv_agent`` via ``get_daiv_agent_kwargs``,
    so the resolved primary model and thinking level are checked at the ``create_daiv_agent`` boundary.
    """

    async def test_max_label_resolves_to_max_model(self, stub_base_init):
        """``daiv-max`` label → primary model is ``site_settings.agent_max_model_name`` and
        thinking level is ``site_settings.agent_max_thinking_level``. The repo-config model
        is preserved as a fallback so the run degrades cleanly on provider outage."""
        captured = await _run_addressor(labels=["daiv-max"])

        model_names = captured["model_names"]
        assert model_names[0] == site_settings.agent_max_model_name
        assert captured["thinking_level"] == site_settings.agent_max_thinking_level
        # The repo-configured model survives as the next fallback in the chain — required
        # so a flaky max-model provider doesn't take the run down with it.
        assert RepositoryConfig().models.agent.model in model_names[1:]

    async def test_max_label_case_insensitive(self, stub_base_init):
        """Label matching must be case-insensitive — GitHub UIs upper-case labels freely."""
        captured = await _run_addressor(labels=["DAIV-MAX"])
        assert captured["model_names"][0] == site_settings.agent_max_model_name
        assert captured["thinking_level"] == site_settings.agent_max_thinking_level

    async def test_no_max_label_uses_repo_config_model(self, stub_base_init):
        """Without ``daiv-max`` the resolved primary model must come from the repo's
        ``AgentModelConfig`` — proving the ``use_max`` branch is the *only* path to the
        max model and isn't accidentally engaged on every webhook."""
        captured = await _run_addressor(labels=["daiv"])
        repo_agent_cfg = RepositoryConfig().models.agent

        assert captured["model_names"][0] == repo_agent_cfg.model
        assert captured["thinking_level"] == repo_agent_cfg.thinking_level
        # The max model must NOT leak into a non-max run.
        assert site_settings.agent_max_model_name not in captured["model_names"]


@contextmanager
def _issue_run(agent, **options):
    """``addressor_run`` for the issue addressor, also yielding the persist mock and the armed watches."""
    armed: list[dict] = []
    persist = AsyncMock()
    with (
        addressor_run(IssueAddressorManager, agent, **options) as run,
        patch("codebase.managers.issue_addressor.PipelineWatch", watch_recorder(armed)),
        patch("sessions.services.apersist_session_ref", persist),
    ):
        run.armed, run.persist = armed, persist
        yield run


class TestIssueAfterRunMatrix:
    @pytest.mark.django_db(transaction=True)
    async def test_it_runs_while_another_holder_has_the_session_slot(self, captured_client):
        """No session lock: the run proceeds while another holder has the slot."""
        thread_id = str(uuid.uuid4())
        await Session.objects.acreate(
            thread_id=thread_id, origin=SessionOrigin.ISSUE_WEBHOOK, repo_id="owner/repo", active_run_id="chat-run"
        )
        agent = addressor_agent(return_value={"messages": [AIMessage(content="done")]})

        with _issue_run(agent):
            await IssueAddressorManager.address_issue(
                issue=_issue(labels=[BOT_LABEL]), runtime_ctx=_ctx(), thread_id=thread_id
            )

        agent.ainvoke.assert_awaited_once()
        assert (await Session.objects.aget(thread_id=thread_id)).active_run_id == "chat-run"

    async def test_a_successful_run_persists_the_ref_and_arms_the_watch(self, captured_client):
        mr = _merge_request()
        agent = addressor_agent(
            return_value={"messages": [AIMessage(content="done")]},
            state_values={"merge_request": mr, "published": True},
        )
        ctx = _ctx()
        ctx.acting_user_id = 7

        with _issue_run(agent) as run:
            await IssueAddressorManager.address_issue(
                issue=_issue(labels=[BOT_LABEL]), runtime_ctx=ctx, thread_id="t-issue"
            )

        run.persist.assert_awaited_once_with(thread_id="t-issue", current_ref="main", merge_request=mr)
        assert run.armed == [{"repo_id": "owner/repo", "merge_request": mr, "published": True, "user_id": 7}]
        run.recover.assert_not_awaited()
        [reply] = captured_client.create_issue_comment.call_args_list
        assert reply.args[2] == "done"

    async def test_an_agent_error_recovers_a_draft_says_so_and_re_raises(self, captured_client):
        with (
            _issue_run(addressor_agent(side_effect=RuntimeError("boom")), draft_published=True) as run,
            pytest.raises(RuntimeError, match="boom"),
        ):
            await IssueAddressorManager.address_issue(issue=_issue(labels=[BOT_LABEL]), runtime_ctx=_ctx())

        assert run.recover.await_args.kwargs == {"entity_label": "issue", "entity_id": 42}
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
            _issue_run(agent, stub_recovery=False),
            patch(
                "codebase.managers.base.GitChangePublisher",
                publisher_through_backend(created, publishes=_merge_request()),
            ),
            patch("codebase.managers.base.get_repo_ref", return_value="daiv/issue-42"),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await IssueAddressorManager.address_issue(issue=_issue(labels=[BOT_LABEL]), runtime_ctx=_sandbox_ctx())

        assert client.calls_to("run_commands") == [(started[0], ("git push origin HEAD",))]
        [note] = captured_client.create_issue_comment.call_args_list
        assert "To avoid losing progress" in note.args[2]

    async def test_a_missing_model_says_it_cannot_run_yet_and_returns(self, captured_client):
        captured_client.get_issue_comment.return_value = SimpleNamespace(
            notes=[SimpleNamespace(author=SimpleNamespace(username="bob"), id="n1", body="please")]
        )
        with _issue_run(addressor_agent(), kwargs_error=AgentConfigurationError("no model")) as run:
            result = await IssueAddressorManager.address_issue(
                issue=_issue(labels=[BOT_LABEL]), mention_comment_id="c-1", runtime_ctx=_ctx()
            )

        assert result is None
        run.create.assert_not_awaited()
        [note] = captured_client.create_issue_comment.call_args_list
        assert note.args[2] == "@alice I can't run yet: no model"
        assert note.kwargs["reply_to_id"] == "c-1"

    @pytest.mark.parametrize(
        ("label", "prompt"), [(BOT_AUTO_LABEL, ADDRESS_ISSUE_PROMPT), (BOT_LABEL, PLAN_ISSUE_PROMPT)]
    )
    async def test_the_label_picks_the_prompt(self, captured_client, label: str, prompt: str):
        agent = addressor_agent(return_value={"messages": [AIMessage(content="done")]})

        with _issue_run(agent):
            await IssueAddressorManager.address_issue(issue=_issue(labels=[label]), runtime_ctx=_ctx())

        [message] = agent.ainvoke.await_args.args[0]["messages"]
        assert message.content == prompt.format(issue_iid=42)
