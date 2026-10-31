"""The issue addressor must claim the merge request it publishes for the issue's session.

It is the workflow that produces most DAIV-published merge requests, so the issue session is where CI on them is
handled. It runs through the executor with ``arm_watch=True``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage
from webhooks.managers.issue_addressor import IssueAddressorManager

from codebase.base import Issue, User
from tests.unit_tests.webhooks.managers.conftest import addressor_agent, addressor_run

_AUTHOR = User(id=1, username="alice")


async def _run_addressor(*, state_values: dict, run_id: str | None = None) -> SimpleNamespace:
    """Drive an issue run to a clean finish over a canned final state; the seam under test is which post-run
    steps fire, not the agent itself."""
    agent = addressor_agent(return_value={"messages": [AIMessage(content="done")]}, state_values=state_values)
    with (
        addressor_run(agent) as run,
        patch("sessions.executor.run._persist_resolved_agent", AsyncMock()),
        patch("sessions.executor.run._record_measurements", AsyncMock()),
        patch("sessions.executor.run._record_publish_result", AsyncMock()),
    ):
        await IssueAddressorManager.address_issue(
            repo_id="owner/repo",
            issue=Issue(id=1, iid=42, title="t", author=_AUTHOR, labels=["daiv"]),
            thread_id="t-1",
            use_max=False,
            run_id=run_id,
        )
    return run


async def test_a_published_issue_run_claims_the_merge_request_for_the_run_the_webhook_created(stub_base_init):
    mr = SimpleNamespace(merge_request_id=7, source_branch="daiv/issue-42")

    run = await _run_addressor(state_values={"merge_request": mr, "published": True}, run_id="run-42")

    assert run.claimed == [{"run_id": "run-42", "merge_request": mr, "pushed_sha": ""}]


async def test_an_issue_run_that_published_nothing_claims_nothing(stub_base_init):
    run = await _run_addressor(state_values={"merge_request": None, "published": False}, run_id="run-42")

    assert run.claimed == []


async def test_the_claim_reuses_the_state_the_result_needs(stub_base_init):
    """One ``aget_state`` for both the claim and the result — a second read is a wasted Redis round-trip on every
    issue the addressor closes."""
    run = await _run_addressor(state_values={"merge_request": None, "published": False})

    agent = run.create_agent.return_value
    assert agent.aget_state.await_count == 1
    assert run.build_result.call_args.kwargs["snapshot"] is agent.aget_state.return_value
