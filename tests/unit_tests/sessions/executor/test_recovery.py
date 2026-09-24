"""``recover_draft`` is the second place a run hands a merge request to the publisher.

It exists to save the work of a run whose agent crashed, so it is exactly where a stale publish target costs the
most: the turn-end publish has already failed, and this is the last attempt. It applies the same guard
``GitMiddleware`` does — see ``automation.agent.publishers.effective_merge_request``.
"""

from unittest.mock import AsyncMock, Mock, patch

from sessions.executor.recovery import recover_draft

from codebase.base import MergeRequest, User
from tests.unit_tests.conftest import FakeSandboxClient, bound_run_sandbox_client, sandbox_runtime
from tests.unit_tests.sessions.executor.conftest import publisher_through_backend

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


def _mr(*, source_branch: str) -> MergeRequest:
    return MergeRequest(
        repo_id="owner/repo",
        merge_request_id=449,
        source_branch=source_branch,
        target_branch="master",
        title="t",
        description="d",
        author=_AUTHOR,
    )


def _ctx(*, sandbox=None) -> Mock:
    ctx = Mock()
    ctx.repository = Mock(slug="owner/repo")
    ctx.sandbox = sandbox
    ctx.merge_request = None
    ctx.gitrepo = Mock()
    return ctx


def _agent(values: dict) -> Mock:
    agent = Mock()
    agent.aget_state = AsyncMock(return_value=Mock(values=values))
    agent.aupdate_state = AsyncMock()
    return agent


async def _publish(*, checkpointed_mr, current_ref: str) -> Mock:
    """Recover over a checkpoint naming ``checkpointed_mr``; return the publisher class mock."""
    with (
        patch("automation.agent.publishers.GitChangePublisher") as pub_cls,
        patch("codebase.utils.get_repo_ref", return_value=current_ref),
    ):
        pub_cls.return_value.publish = AsyncMock(return_value=Mock(merge_request=None))
        await recover_draft(_ctx(), _agent({"merge_request": checkpointed_mr, "session_id": None}), {}, thread_id="t-1")
    return pub_cls


class TestPublishTarget:
    async def test_it_drops_a_checkpointed_mr_the_workspace_is_not_on(self):
        """Re-publishing onto that branch is what already failed a moment ago — retrying it here just loses the
        work a second time. A fresh draft MR keeps it."""
        pub_cls = await _publish(checkpointed_mr=_mr(source_branch="fix/10-update-dependencies"), current_ref="master")

        kwargs = pub_cls.return_value.publish.await_args.kwargs
        assert kwargs["merge_request"] is None
        assert kwargs["as_draft"] is True

    async def test_it_keeps_the_mr_when_the_workspace_is_on_its_branch(self):
        """The ordinary crash-recovery case: the run was working on its own MR's branch, so the draft belongs on
        that MR rather than in a duplicate."""
        pub_cls = await _publish(checkpointed_mr=_mr(source_branch="fix/10"), current_ref="fix/10")

        kwargs = pub_cls.return_value.publish.await_args.kwargs
        assert kwargs["merge_request"] is not None
        assert kwargs["merge_request"].merge_request_id == 449

    async def test_it_still_recovers_when_the_checkpoint_did_not_revive(self, caplog):
        """``DAIVRedisSerializer`` hands back the raw envelope dict when a ``MergeRequest`` fails to reconstruct (a
        schema drift across a deploy). Raising here would discard the work recovery exists to save, so the dict is
        dropped, loudly, and the run still gets a fresh draft MR."""
        with caplog.at_level("ERROR"):
            pub_cls = await _publish(checkpointed_mr={"source_branch": "fix/10"}, current_ref="fix/10")

        kwargs = pub_cls.return_value.publish.await_args.kwargs
        assert kwargs["merge_request"] is None
        assert kwargs["as_draft"] is True
        assert "revived as dict" in caplog.text
        assert "draft recovery failed" not in caplog.text

    async def test_it_hands_the_publisher_the_runs_thread_id(self):
        pub_cls = await _publish(checkpointed_mr=None, current_ref="master")

        assert pub_cls.call_args.kwargs["thread_id"] == "t-1"


class TestSandboxMode:
    @staticmethod
    async def _recover(client: FakeSandboxClient, session_id: str, *, publisher) -> tuple[bool, Mock]:
        agent = _agent({"merge_request": None, "session_id": session_id})
        with (
            bound_run_sandbox_client(client),
            patch("automation.agent.publishers.GitChangePublisher", publisher),
            patch("codebase.utils.get_repo_ref", return_value="daiv/issue-10"),
        ):
            published = await recover_draft(_ctx(sandbox=sandbox_runtime()), agent, {}, thread_id="t-1")
        return published, agent

    async def test_it_publishes_a_draft_through_the_live_session(self):
        """B7: recovery publishes through the run's live session, without reopening or closing it."""
        client = FakeSandboxClient.opened()
        session_id = client.add_running_session("sess-1")
        created: list = []

        published, agent = await self._recover(
            client, session_id, publisher=publisher_through_backend(created, publishes=_DRAFT_MR)
        )

        assert published is True
        assert created[0].target == (None, True)
        assert client.calls_to("run_commands") == [(session_id, ("git push origin HEAD",))]
        assert client.method_names() == ["run_commands"]
        agent.aupdate_state.assert_awaited_once_with(config={}, values={"merge_request": _DRAFT_MR})

    async def test_a_failed_publish_reports_no_draft(self, caplog):
        """B7: a publish that raises is logged and reported as no draft, never re-raised."""
        client = FakeSandboxClient.opened()
        session_id = client.add_running_session("sess-1")
        publisher = Mock()
        publisher.return_value.publish = AsyncMock(side_effect=RuntimeError("push rejected"))

        with caplog.at_level("ERROR", logger="daiv.sessions"):
            published, agent = await self._recover(client, session_id, publisher=publisher)

        assert published is False
        agent.aupdate_state.assert_not_awaited()
        assert "draft recovery failed after an agent error for thread_id=t-1" in caplog.text
