import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain.tools import ToolRuntime
from pydantic import ValidationError

from automation.agent.delegation import (
    CANCEL_DELEGATED_JOBS_TOOL_NAME,
    CHECK_DELEGATED_JOBS_TOOL_NAME,
    DELEGATE_JOBS_TOOL_NAME,
    BatchStatus,
    CancelResult,
    Checkout,
    DelegatedTarget,
    DelegationError,
    DelegationResult,
    FailedLeg,
    LegStatus,
    StartedLeg,
)
from automation.agent.middlewares.delegate_jobs import (
    DELEGATE_JOBS_SYSTEM_PROMPT,
    DelegateJobsMiddleware,
    DelegateTarget,
)
from automation.agent.references import RefIn

REPO = SimpleNamespace(repo_id="acme/coord", ref="feature")


def _runtime() -> ToolRuntime:
    return ToolRuntime(
        state={},
        context=SimpleNamespace(repo=REPO),
        config={"configurable": {"thread_id": "thread-1"}},
        stream_writer=Mock(),
        tool_call_id="c1",
        store=None,
    )


def _delegator(result: DelegationResult | None = None, *, side_effect: Exception | None = None) -> Mock:
    delegator = Mock()
    delegator.adelegate = AsyncMock(return_value=result, side_effect=side_effect)
    return delegator


def _tool(delegator: Mock, name: str = DELEGATE_JOBS_TOOL_NAME):
    [tool] = [t for t in DelegateJobsMiddleware(delegator=delegator).tools if t.name == name]
    return tool


async def _call(delegator: Mock, *, goal: str = "Bump auth", targets=None):
    targets = targets or [DelegateTarget(repo_id="acme/api", prompt="Bump auth to 3.2.1.")]
    return json.loads(await _tool(delegator).coroutine(goal=goal, targets=targets, runtime=_runtime()))


def _leg_status(thread_id: str = "t2", status: str = "running") -> LegStatus:
    return LegStatus("acme/api", "", thread_id, f"https://daiv.example/sessions/{thread_id}/", status)


async def _check(delegator: Mock, **kwargs):
    return json.loads(await _tool(delegator, CHECK_DELEGATED_JOBS_TOOL_NAME).coroutine(**kwargs))


async def _cancel(delegator: Mock, **kwargs):
    return json.loads(await _tool(delegator, CANCEL_DELEGATED_JOBS_TOOL_NAME).coroutine(**kwargs))


def test_the_middleware_adds_the_delegate_check_and_cancel_tools():
    assert [t.name for t in DelegateJobsMiddleware(delegator=_delegator()).tools] == [
        DELEGATE_JOBS_TOOL_NAME,
        CHECK_DELEGATED_JOBS_TOOL_NAME,
        CANCEL_DELEGATED_JOBS_TOOL_NAME,
    ]


def test_every_target_needs_a_prompt():
    with pytest.raises(ValidationError):
        _tool(_delegator()).args_schema.model_validate({"goal": "g", "targets": [{"repo_id": "acme/api"}]})


async def test_the_targets_reach_the_delegator_trimmed_with_the_runs_own_checkout():
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
        checkout=Checkout(repo_id="acme/coord", ref="feature"),
        goal="Bump auth",
        targets=[
            DelegatedTarget(repo_id="acme/api", ref="", prompt="Bump it."),
            DelegatedTarget(repo_id="acme/web", ref="release", prompt="Rebuild."),
        ],
    )


async def test_a_targets_references_reach_the_delegator():
    delegator = _delegator(DelegationResult(batch_id="b1", started=[StartedLeg("acme/api", "", "t2", "https://x/t2")]))
    sentry = RefIn(key="DAIV-1V", provider="sentry", url="https://sentry.example/issues/1", relation="closes")

    await _call(delegator, targets=[DelegateTarget(repo_id="acme/api", prompt="Fix it.", references=[sentry])])

    [target] = delegator.adelegate.await_args.kwargs["targets"]
    assert target.references == (sentry,)


async def test_it_returns_the_started_and_failed_legs():
    result = DelegationResult(
        batch_id="b1",
        started=[StartedLeg("acme/api", "", "t2", "https://daiv.example/sessions/t2/", starts_after_this_turn=True)],
        failed=[FailedLeg("acme/web", "", "denied")],
    )

    assert await _call(_delegator(result)) == {
        "batch_id": "b1",
        "started": [
            {
                "repo_id": "acme/api",
                "ref": "",
                "thread_id": "t2",
                "session_url": "https://daiv.example/sessions/t2/",
                "starts_after_this_turn": True,
            }
        ],
        "failed": [{"repo_id": "acme/web", "ref": "", "error": "denied", "still_runs": False}],
    }


async def test_when_nothing_started_it_says_no_resume_will_come():
    payload = await _call(_delegator(DelegationResult(batch_id=None, failed=[FailedLeg("acme/web", "", "denied")])))

    assert payload["started"] == []
    assert "no resume will come" in payload["note"]


async def test_when_only_an_untracked_leg_runs_it_says_not_to_redo_its_work():
    result = DelegationResult(batch_id="b1", failed=[FailedLeg("acme/web", "", "lost track", still_runs=True)])

    payload = await _call(_delegator(result))

    assert payload["started"] == []
    assert payload["failed"][0]["still_runs"] is True
    assert payload["note"].startswith("No resume will come.")
    assert "never redo its work" in payload["note"]


async def test_a_refused_call_returns_the_reason():
    payload = await _call(_delegator(side_effect=DelegationError("listed twice")))

    assert payload == {"error": "listed twice"}


async def test_an_unexpected_failure_is_logged_and_reported_as_ours(caplog):
    with caplog.at_level("ERROR", logger="daiv.tools"):
        payload = await _call(_delegator(side_effect=RuntimeError("db down")))

    assert "failed on DAIV's side" in payload["error"]
    assert "delegate_jobs: unexpected failure" in caplog.text


async def test_it_appends_the_coordinator_workflow_to_the_system_prompt():
    request = Mock(system_prompt="BASE")
    request.override = Mock(return_value="overridden")
    handler = AsyncMock(return_value="response")

    assert await DelegateJobsMiddleware(delegator=_delegator()).awrap_model_call(request, handler) == "response"

    request.override.assert_called_once_with(system_prompt=f"BASE\n\n{DELEGATE_JOBS_SYSTEM_PROMPT}")
    handler.assert_awaited_once_with("overridden")


@pytest.mark.parametrize(("batch_id", "asked"), [(" b1 ", "b1"), ("", None)], ids=["one-batch", "latest-batches"])
async def test_check_reports_the_batches_the_delegator_finds(batch_id, asked):
    delegator = _delegator()
    delegator.astatus = AsyncMock(return_value=[BatchStatus("b1", finished=False, legs=[_leg_status()])])

    payload = await _check(delegator, batch_id=batch_id)

    delegator.astatus.assert_awaited_once_with(batch_id=asked)
    assert payload == {
        "batches": [
            {
                "batch_id": "b1",
                "finished": False,
                "legs": [
                    {
                        "repo_id": "acme/api",
                        "ref": "",
                        "thread_id": "t2",
                        "session_url": "https://daiv.example/sessions/t2/",
                        "status": "running",
                        "branch": "",
                        "merge_request_url": "",
                        "reply": "",
                    }
                ],
            }
        ]
    }


async def test_check_says_when_the_session_delegated_nothing():
    delegator = _delegator()
    delegator.astatus = AsyncMock(return_value=[])

    assert await _check(delegator) == {"batches": [], "note": "This session has delegated no sub-jobs."}


async def test_check_returns_why_a_refused_call_was_refused():
    delegator = _delegator()
    delegator.astatus = AsyncMock(side_effect=DelegationError("no batch b9"))

    assert await _check(delegator, batch_id="b9") == {"error": "no batch b9"}


def test_cancel_needs_a_batch_but_not_the_legs():
    schema = _tool(_delegator(), CANCEL_DELEGATED_JOBS_TOOL_NAME).args_schema

    with pytest.raises(ValidationError):
        schema.model_validate({"thread_ids": ["t2"]})
    assert schema.model_validate({"batch_id": "b1"}).thread_ids is None


async def test_cancel_hands_the_trimmed_batch_and_legs_to_the_delegator():
    delegator = _delegator()
    batch = BatchStatus("b1", finished=True, legs=[_leg_status(status="cancelled")])
    delegator.acancel = AsyncMock(return_value=CancelResult(batch, ["t2"], resumes=False))

    payload = await _cancel(delegator, batch_id=" b1 ", thread_ids=[" t2 ", " "])

    delegator.acancel.assert_awaited_once_with(batch_id="b1", thread_ids=["t2"])
    assert payload["batch_id"] == "b1"
    assert payload["cancelled"] == ["t2"]
    assert payload["legs"][0]["status"] == "cancelled"


async def test_cancel_without_legs_cancels_the_whole_batch():
    delegator = _delegator()
    delegator.acancel = AsyncMock(return_value=CancelResult(BatchStatus("b1", finished=True, legs=[]), [], True))

    await _cancel(delegator, batch_id="b1")

    delegator.acancel.assert_awaited_once_with(batch_id="b1", thread_ids=[])


@pytest.mark.parametrize(
    ("thread_ids", "cancelled", "resumes", "finished", "note"),
    [
        (["t2"], [], True, False, "the legs you named had already finished"),
        (None, [], True, True, "every leg of this batch had already finished"),
        (None, ["t2"], False, False, "no resume will come"),
        (None, ["t2"], True, False, "once the legs still queued, running or stopping have finished"),
        (None, ["t2"], True, True, "once this turn ends"),
    ],
    ids=["named-legs-done", "batch-done", "every-leg-cancelled", "others-still-run", "batch-finished"],
)
async def test_cancel_says_whether_the_batch_still_resumes_the_coordinator(
    thread_ids, cancelled, resumes, finished, note
):
    delegator = _delegator()
    delegator.acancel = AsyncMock(
        return_value=CancelResult(BatchStatus("b1", finished=finished, legs=[]), cancelled, resumes)
    )

    assert note in (await _cancel(delegator, batch_id="b1", thread_ids=thread_ids))["note"]


async def test_an_unexpected_cancel_failure_is_logged_and_reported_as_ours(caplog):
    delegator = _delegator()
    delegator.acancel = AsyncMock(side_effect=RuntimeError("db down"))

    with caplog.at_level("ERROR", logger="daiv.tools"):
        payload = await _cancel(delegator, batch_id="b1")

    assert "failed on DAIV's side" in payload["error"]
    assert "cancel_delegated_jobs: unexpected failure" in caplog.text
