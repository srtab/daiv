import json
from unittest.mock import AsyncMock, Mock

import pytest
from langchain.tools import ToolRuntime
from pydantic import ValidationError

from automation.agent.delegation import (
    DELEGATE_JOBS_TOOL_NAME,
    DelegatedTarget,
    DelegationError,
    DelegationResult,
    FailedLeg,
    StartedLeg,
)
from automation.agent.middlewares.delegate_jobs import (
    DELEGATE_JOBS_SYSTEM_PROMPT,
    DelegateJobsMiddleware,
    DelegateTarget,
)

THREAD_ID = "thread-1"


def _runtime(thread_id: str | None = THREAD_ID) -> ToolRuntime:
    return ToolRuntime(
        state={},
        context=Mock(),
        config={"configurable": {"thread_id": thread_id} if thread_id else {}},
        stream_writer=Mock(),
        tool_call_id="c1",
        store=None,
    )


def _delegator(result: DelegationResult | None = None, *, side_effect: Exception | None = None) -> Mock:
    delegator = Mock()
    delegator.adelegate = AsyncMock(return_value=result, side_effect=side_effect)
    return delegator


def _tool(delegator: Mock):
    [tool] = DelegateJobsMiddleware(delegator=delegator).tools
    return tool


async def _call(delegator: Mock, *, goal: str = "Bump auth", targets=None, runtime: ToolRuntime | None = None):
    targets = targets or [DelegateTarget(repo_id="acme/api", prompt="Bump auth to 3.2.1.")]
    return json.loads(await _tool(delegator).coroutine(goal=goal, targets=targets, runtime=runtime or _runtime()))


def test_the_middleware_adds_the_delegate_jobs_tool():
    assert _tool(_delegator()).name == DELEGATE_JOBS_TOOL_NAME


def test_every_target_needs_a_prompt():
    with pytest.raises(ValidationError):
        _tool(_delegator()).args_schema.model_validate({"goal": "g", "targets": [{"repo_id": "acme/api"}]})


async def test_the_targets_reach_the_delegator_trimmed_with_a_blank_ref_for_the_default_branch():
    delegator = _delegator(DelegationResult(batch_id="b1", started=[StartedLeg("acme/api", "", "t2", "https://x/t2")]))

    await _call(
        delegator,
        goal=" Bump auth ",
        targets=[
            DelegateTarget(repo_id=" acme/api ", prompt=" Bump it. "),
            DelegateTarget(repo_id="acme/web", ref=" release ", prompt="Rebuild."),
        ],
    )

    delegator.adelegate.assert_awaited_once_with(
        thread_id=THREAD_ID,
        goal="Bump auth",
        targets=[
            DelegatedTarget(repo_id="acme/api", ref="", prompt="Bump it."),
            DelegatedTarget(repo_id="acme/web", ref="release", prompt="Rebuild."),
        ],
    )


async def test_it_returns_the_started_and_failed_legs():
    result = DelegationResult(
        batch_id="b1",
        started=[StartedLeg("acme/api", "", "t2", "https://daiv.example/sessions/t2/")],
        failed=[FailedLeg("acme/web", "", "denied")],
    )

    assert await _call(_delegator(result)) == {
        "batch_id": "b1",
        "started": [
            {"repo_id": "acme/api", "ref": "", "thread_id": "t2", "session_url": "https://daiv.example/sessions/t2/"}
        ],
        "failed": [{"repo_id": "acme/web", "ref": "", "error": "denied"}],
    }


async def test_when_nothing_started_it_says_no_resume_will_come():
    payload = await _call(_delegator(DelegationResult(batch_id=None, failed=[FailedLeg("acme/web", "", "denied")])))

    assert payload["started"] == []
    assert "no resume will come" in payload["note"]


async def test_a_refused_call_returns_the_reason():
    payload = await _call(_delegator(side_effect=DelegationError("listed twice")))

    assert payload == {"error": "listed twice"}


async def test_an_unexpected_failure_is_logged_and_reported_as_ours(caplog):
    with caplog.at_level("ERROR", logger="daiv.tools"):
        payload = await _call(_delegator(side_effect=RuntimeError("db down")))

    assert "failed on DAIV's side" in payload["error"]
    assert "delegate_jobs: unexpected failure" in caplog.text


async def test_a_run_without_a_session_cannot_delegate():
    delegator = _delegator()

    payload = await _call(delegator, runtime=_runtime(thread_id=None))

    assert "no session" in payload["error"]
    delegator.adelegate.assert_not_awaited()


async def test_it_appends_the_coordinator_workflow_to_the_system_prompt():
    request = Mock(system_prompt="BASE")
    request.override = Mock(return_value="overridden")
    handler = AsyncMock(return_value="response")

    assert await DelegateJobsMiddleware(delegator=_delegator()).awrap_model_call(request, handler) == "response"

    request.override.assert_called_once_with(system_prompt=f"BASE\n\n{DELEGATE_JOBS_SYSTEM_PROMPT}")
    handler.assert_awaited_once_with("overridden")
