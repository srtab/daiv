"""The issue addressor must arm the CI watch on the merge request it publishes.

It is the workflow that produces most DAIV-published merge requests, and for a long time it bypassed the job
runner and never armed the watch. It now runs through the executor with ``arm_watch=True``.
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage
from webhooks.managers.issue_addressor import IssueAddressorManager

from codebase.base import Issue, User
from tests.unit_tests.webhooks.managers.conftest import addressor_agent, addressor_run

_AUTHOR = User(id=1, username="alice")


async def _run_addressor(*, state_values: dict) -> SimpleNamespace:
    """Drive an issue run to a clean finish over a canned final state; the seam under test is which post-run
    steps fire, not the agent itself."""
    agent = addressor_agent(return_value={"messages": [AIMessage(content="done")]}, state_values=state_values)
    with addressor_run(agent) as run:
        await IssueAddressorManager.address_issue(
            repo_id="owner/repo", issue=Issue(id=1, iid=42, title="t", author=_AUTHOR, labels=["daiv"])
        )
    return run


async def test_a_published_issue_run_arms_the_watch(stub_base_init):
    mr = SimpleNamespace(merge_request_id=7, source_branch="daiv/issue-42")

    run = await _run_addressor(state_values={"merge_request": mr, "published": True})

    [armed] = run.armed
    assert armed["repo_id"] == "owner/repo"
    assert armed["merge_request"] is mr
    assert armed["published"] is True
    assert armed["user_id"] is None


async def test_an_issue_run_that_published_nothing_reports_it(stub_base_init):
    """The arm is still called — it owns the give-up decision — but with ``published`` false, so a no-op run
    cannot re-arm a watch."""
    run = await _run_addressor(state_values={"merge_request": None, "published": False})

    [armed] = run.armed
    assert armed["published"] is False


async def test_the_watch_arm_reuses_the_state_the_result_needs(stub_base_init):
    """One ``aget_state`` for both the arm and the result — a second read is a wasted Redis round-trip on every
    issue the addressor closes."""
    run = await _run_addressor(state_values={"merge_request": None, "published": False})

    agent = run.create_agent.return_value
    assert agent.aget_state.await_count == 1
    assert run.build_result.await_args.kwargs["snapshot"] is agent.aget_state.return_value
