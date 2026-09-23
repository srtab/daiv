from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from codebase.base import Issue, MergeRequest, User
from codebase.context import SandboxRuntime
from codebase.managers.issue_addressor import IssueAddressorManager
from core.sandbox.client import reset_run_sandbox_client, set_run_sandbox_client
from core.sandbox.command_policy import SandboxCommandPolicy
from core.sandbox.schemas import StartSessionRequest
from tests.unit_tests.conftest import FakeSandboxClient

_AUTHOR = User(id=1, username="alice")

_DRAFT_MR = MergeRequest(
    repo_id="owner/repo",
    merge_request_id=7,
    source_branch="daiv/issue-10",
    target_branch="main",
    title="t",
    description="d",
    author=_AUTHOR,
    draft=True,
)


def _publisher_through_backend(created: list):
    """A publisher stand-in that pushes through whatever backend it is handed."""

    class _Publisher:
        def __init__(self, ctx, *, sandbox_backend, thread_id):
            self.sandbox_backend = sandbox_backend
            created.append(self)

        async def publish(self, *, merge_request, as_draft):
            self.target = (merge_request, as_draft)
            await self.sandbox_backend.run_commands(["git push origin HEAD"], fail_fast=True)
            return SimpleNamespace(merge_request=_DRAFT_MR, protected_branch_fallback_source=None)

    return _Publisher


def _sandbox_ctx() -> Mock:
    ctx = Mock()
    ctx.repository = Mock(slug="owner/repo")
    ctx.merge_request = None
    ctx.gitrepo = Mock()
    ctx.sandbox = SandboxRuntime(
        base_image="python:3.12", memory_bytes=None, cpus=None, env_vars={}, command_policy=SandboxCommandPolicy()
    )
    return ctx


async def _recover(client: FakeSandboxClient, session_id: str, *, publisher) -> tuple:
    manager = IssueAddressorManager(
        issue=Issue(id=1, iid=10, title="t", author=_AUTHOR), runtime_ctx=_sandbox_ctx(), thread_id="t-1"
    )
    agent = Mock()
    agent.aget_state = AsyncMock(return_value=Mock(values={"merge_request": None, "session_id": session_id}))
    agent.aupdate_state = AsyncMock()
    token = set_run_sandbox_client(client)
    try:
        with (
            patch("codebase.managers.base.GitChangePublisher", publisher),
            patch("codebase.managers.base.get_repo_ref", return_value="daiv/issue-10"),
        ):
            published = await manager._recover_draft(agent, {}, entity_label="issue", entity_id=10)
    finally:
        reset_run_sandbox_client(token)
    return published, agent


class TestRecoverDraftInSandboxMode:
    async def test_it_publishes_a_draft_through_the_live_session(self, stub_base_init):
        """B7: recovery publishes through the run's live session, without reopening or closing it."""
        client = FakeSandboxClient()
        session_id = await client.start_session(StartSessionRequest(base_image="python:3.12"))
        created: list = []

        published, agent = await _recover(client, session_id, publisher=_publisher_through_backend(created))

        assert published is True
        assert created[0].target == (None, True)
        assert client.calls_to("run_commands") == [(session_id, ("git push origin HEAD",))]
        assert client.method_names() == ["start_session", "run_commands"]
        agent.aupdate_state.assert_awaited_once_with(config={}, values={"merge_request": _DRAFT_MR})

    async def test_a_failed_publish_reports_no_draft(self, stub_base_init):
        """B7: a publish that raises is logged and reported as no draft, never re-raised."""
        client = FakeSandboxClient()
        session_id = await client.start_session(StartSessionRequest(base_image="python:3.12"))
        publisher = Mock()
        publisher.return_value.publish = AsyncMock(side_effect=RuntimeError("push rejected"))

        published, agent = await _recover(client, session_id, publisher=publisher)

        assert published is False
        agent.aupdate_state.assert_not_awaited()
