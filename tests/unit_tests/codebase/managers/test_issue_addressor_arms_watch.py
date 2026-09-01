"""The issue addressor must arm the CI watch on the merge request it publishes.

It bypasses ``run_job_task`` entirely (it calls ``create_daiv_agent`` directly), and it is the
workflow that produces most DAIV-published merge requests — so for a long time the watch was
armed on the one path that produced the fewest of them.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from codebase.base import GitPlatform, Issue, User
from codebase.managers.base import BaseManager
from codebase.managers.issue_addressor import IssueAddressorManager
from codebase.repo_config import RepositoryConfig
from tests.unit_tests.sessions.conftest import watch_recorder

_AUTHOR = User(id=1, username="alice")


def _ctx(*, acting_user_id: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        repository=SimpleNamespace(slug="owner/repo"),
        git_platform=GitPlatform.GITLAB,
        bot_username="daiv-bot",
        config=RepositoryConfig(),
        acting_user_id=acting_user_id,
    )


@pytest.fixture
def stub_base_init():
    def _init(self, *, runtime_ctx, thread_id):
        self.ctx = runtime_ctx
        self.thread_id = thread_id
        self.client = MagicMock()
        self.store = MagicMock()
        self.git_manager = MagicMock()

    with patch.object(BaseManager, "__init__", _init):
        yield


@asynccontextmanager
async def _noop_checkpointer():
    yield MagicMock()


async def _run_addressor(*, state_values: dict, acting_user_id: int | None = None) -> list[dict]:
    """Drive ``_address_issue`` to a clean finish over a canned final state, capturing the
    watch-arm calls. The agent, checkpointer and LangSmith config are all stubbed — the seam
    under test is which post-run hooks fire, not the agent itself."""
    armed: list[dict] = []

    agent = MagicMock()
    agent.get_name.return_value = "daiv"
    agent.ainvoke = AsyncMock(return_value={"messages": [AIMessage(content="done")]})
    agent.aget_state = AsyncMock(return_value=SimpleNamespace(values=state_values))

    with (
        patch("codebase.managers.issue_addressor.open_checkpointer", _noop_checkpointer),
        patch("codebase.managers.issue_addressor.create_daiv_agent", AsyncMock(return_value=agent)),
        patch("codebase.managers.issue_addressor.build_langsmith_config", return_value={}),
        patch("codebase.managers.issue_addressor.PipelineWatch", watch_recorder(armed)),
        patch.object(BaseManager, "_build_agent_result", AsyncMock(return_value={})),
        patch.object(IssueAddressorManager, "_leave_comment"),
    ):
        await IssueAddressorManager.address_issue(
            issue=Issue(id=1, iid=42, title="t", author=_AUTHOR, labels=["daiv"]),
            runtime_ctx=_ctx(acting_user_id=acting_user_id),
        )

    return armed


async def test_a_published_issue_run_arms_the_watch(stub_base_init):
    mr = SimpleNamespace(merge_request_id=7, source_branch="daiv/issue-42")

    armed = await _run_addressor(state_values={"merge_request": mr, "published": True}, acting_user_id=3)

    assert len(armed) == 1
    assert armed[0]["repo_id"] == "owner/repo"
    assert armed[0]["merge_request"] is mr
    assert armed[0]["published"] is True
    assert armed[0]["user_id"] == 3


async def test_an_issue_run_that_published_nothing_reports_it(stub_base_init):
    """The arm is still called — it owns the give-up decision — but with ``published`` false,
    so a no-op run cannot re-arm a watch."""
    armed = await _run_addressor(state_values={"merge_request": None, "published": False})

    assert len(armed) == 1
    assert armed[0]["published"] is False


async def test_the_watch_arm_reuses_the_state_the_result_needs(stub_base_init):
    """One ``aget_state`` for both the arm and ``_build_agent_result`` — a second read here is
    a wasted Redis round-trip on every issue the addressor closes."""
    agent = MagicMock()
    agent.get_name.return_value = "daiv"
    agent.ainvoke = AsyncMock(return_value={"messages": [AIMessage(content="done")]})
    agent.aget_state = AsyncMock(return_value=SimpleNamespace(values={"merge_request": None, "published": False}))

    build = AsyncMock(return_value={})
    with (
        patch("codebase.managers.issue_addressor.open_checkpointer", _noop_checkpointer),
        patch("codebase.managers.issue_addressor.create_daiv_agent", AsyncMock(return_value=agent)),
        patch("codebase.managers.issue_addressor.build_langsmith_config", return_value={}),
        patch("codebase.managers.issue_addressor.PipelineWatch", watch_recorder([])),
        patch.object(BaseManager, "_build_agent_result", build),
        patch.object(IssueAddressorManager, "_leave_comment"),
    ):
        await IssueAddressorManager.address_issue(
            issue=Issue(id=1, iid=42, title="t", author=_AUTHOR, labels=["daiv"]), runtime_ctx=_ctx()
        )

    assert agent.aget_state.await_count == 1
    assert build.await_args.kwargs["snapshot"] is agent.aget_state.return_value
