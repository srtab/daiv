from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import Mock

import httpx
import pytest
from langchain.agents.middleware import ExtendedModelResponse, ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from automation.agent.constants import REPO_PATH
from automation.agent.middlewares.loop_breaker import LoopBreakerMiddleware
from automation.agent.middlewares.step_budget import StepBudgetMiddleware
from automation.agent.middlewares.verification import VerificationMiddleware
from automation.agent.synthetic import is_synthetic
from core.sandbox.schemas import RunCommandResult, RunCommandsResponse
from tests.unit_tests.conftest import FakeWorkspace

AGENT_DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
LOCK_DIFF = "diff --git a/uv.lock b/uv.lock\n--- a/uv.lock\n+++ b/uv.lock\n@@ -1 +1 @@\n-v1\n+v2\n"


@dataclass
class FakeRepo:
    """The run's sandbox as both ``workspace.git`` and ``workspace.bash``: ``diff`` is the working tree, ``failing``
    the commands that fail on it, ``failing_on_start`` those that fail on the starting code (while stashed), and
    ``checks_leave`` the tree a check run leaves behind."""

    changed: list[str] = field(default_factory=lambda: ["a.py"])
    diff: str = AGENT_DIFF
    failing: set[str] = field(default_factory=set)
    failing_on_start: set[str] = field(default_factory=set)
    checks_leave: str | None = None
    error: Exception | None = None
    stashed: bool = False
    stashes: int = 0
    check_runs: int = 0

    async def get_changed_files(self) -> list[str]:
        return list(self.changed)

    async def get_diff(self) -> str:
        return self.diff

    async def pending_merge(self) -> None:
        return None

    async def stash_all(self, message: str) -> bool:
        self.stashes += 1
        self.stashed = True
        return True

    async def restore_stash(self) -> None:
        self.stashed = False

    async def run_commands(self, commands, *, fail_fast, timeout=None) -> RunCommandsResponse:
        if self.error is not None:
            raise self.error
        declared = [command.removeprefix(f"cd {REPO_PATH} && ") for command in commands]
        failing = self.failing_on_start if self.stashed else self.failing
        if not self.stashed:
            self.check_runs += 1
            if self.checks_leave is not None:
                self.diff = self.checks_leave
        return RunCommandsResponse(
            results=[
                RunCommandResult(command=command, output=f"{name} output", exit_code=1 if name in failing else 0)
                for command, name in zip(commands, declared, strict=True)
            ]
        )


def _request(messages: list | None = None) -> ModelRequest:
    return ModelRequest(
        model=GenericFakeChatModel(messages=iter([])),
        messages=messages or [HumanMessage(content="task")],
        system_prompt="base",
    )


def _final(text: str = "done") -> AIMessage:
    return AIMessage(content=text)


def _tool_call(call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": call_id}])


def _scripted(*replies: AIMessage):
    """A handler that answers the n-th model call with ``replies[n]`` and records every request it gets."""
    seen: list[ModelRequest] = []

    async def handler(request: ModelRequest) -> ModelResponse:
        seen.append(request)
        return ModelResponse(result=[replies[len(seen) - 1]])

    return handler, seen


def _middleware(
    repo: FakeRepo, commands: tuple[str, ...] = ("make test",), *, finalizing: bool = False, terminated: bool = False
) -> VerificationMiddleware:
    step_budget = Mock(spec=StepBudgetMiddleware)
    step_budget.is_finalizing.return_value = finalizing
    loop_breaker = LoopBreakerMiddleware(terminal="finalize")
    loop_breaker.terminated = terminated
    return VerificationMiddleware(
        workspace=FakeWorkspace(git=repo, bash=repo),
        commands=commands,
        step_budget=step_budget,
        loop_breaker=loop_breaker,
    )


async def _fails_on_reminder(request: ModelRequest) -> ModelResponse:
    if is_synthetic(request.messages[-1]):
        raise RuntimeError("provider down")
    return ModelResponse(result=[_final()])


def _outcome(result) -> dict:
    assert isinstance(result, ExtendedModelResponse)
    return result.command.update["verification"]


async def test_tool_call_response_passes_through_without_running_checks():
    repo = FakeRepo(failing={"make test"})
    reply = _tool_call()
    handler, seen = _scripted(reply)

    result = await _middleware(repo).awrap_model_call(_request(), handler)

    assert not isinstance(result, ExtendedModelResponse)
    assert result.result == [reply]
    assert repo.check_runs == 0
    assert len(seen) == 1


async def test_final_answer_with_no_changes_runs_nothing():
    repo = FakeRepo(changed=[], diff="", failing={"make test"})
    reply = _final()
    handler, seen = _scripted(reply)

    result = await _middleware(repo).awrap_model_call(_request(), handler)

    assert not isinstance(result, ExtendedModelResponse)
    assert result.result == [reply]
    assert repo.check_runs == 0
    assert len(seen) == 1


async def test_passing_checks_record_passed_outcome():
    repo = FakeRepo()
    reply = _final()
    handler, seen = _scripted(reply)

    result = await _middleware(repo).awrap_model_call(_request(), handler)

    outcome = _outcome(result)
    assert outcome["status"] == "passed"
    assert outcome["rounds"] == 0
    assert outcome["fingerprint"]
    assert [command["command"] for command in outcome["commands"]] == ["make test"]
    assert result.model_response.result == [reply]
    assert len(seen) == 1


async def test_failing_check_sends_reminder_and_keeps_first_answer():
    repo = FakeRepo(failing={"make test"})
    first_final, second_reply = _final("first"), _final("second")
    handler, seen = _scripted(first_final, second_reply)
    request = _request()

    result = await _middleware(repo).awrap_model_call(request, handler)

    first, reminder, second = result.model_response.result
    assert (first, second) == (first_final, second_reply)
    assert is_synthetic(reminder)
    assert "make test: exit code 1" in reminder.content
    assert "make test output" in reminder.content
    assert seen[1].messages == [*request.messages, first_final, reminder]


async def test_failures_stop_after_two_fix_rounds():
    repo = FakeRepo(failing={"make test"})
    middleware = _middleware(repo)
    for fix in ("first fix", "second fix"):
        handler, _ = _scripted(_final(), _tool_call())
        result = await middleware.awrap_model_call(_request(), handler)
        assert not isinstance(result, ExtendedModelResponse)
        repo.diff += f"+{fix}\n"

    handler, seen = _scripted(_final())
    result = await middleware.awrap_model_call(_request(), handler)

    outcome = _outcome(result)
    assert outcome["status"] == "failed"
    assert outcome["rounds"] == 2
    assert repo.check_runs == 3
    assert len(seen) == 1


async def test_failure_already_present_on_starting_code_is_not_sent_back():
    repo = FakeRepo(failing={"make test"}, failing_on_start={"make test"})
    handler, seen = _scripted(_final())

    result = await _middleware(repo).awrap_model_call(_request(), handler)

    outcome = _outcome(result)
    assert outcome["status"] == "passed"
    assert outcome["commands"][0]["already_failing"]
    assert len(seen) == 1


async def test_baseline_runs_once_per_command_per_run():
    repo = FakeRepo(failing={"make test"})
    middleware = _middleware(repo)

    handler, _ = _scripted(_final(), _tool_call())
    await middleware.awrap_model_call(_request(), handler)
    repo.diff += "+another try\n"
    handler, seen = _scripted(_final(), _tool_call())
    await middleware.awrap_model_call(_request(), handler)

    assert repo.check_runs == 2
    assert len(seen) == 2
    assert repo.stashes == 1


async def test_check_that_changes_files_counts_as_failed():
    repo = FakeRepo(checks_leave=AGENT_DIFF + LOCK_DIFF)
    handler, seen = _scripted(_final(), _final())

    result = await _middleware(repo).awrap_model_call(_request(), handler)

    reminder = seen[1].messages[-1]
    assert "running the checks changed these files: uv.lock." in reminder.content
    assert _outcome(result)["status"] == "failed"


async def test_unchanged_tree_after_reminder_is_not_rechecked():
    repo = FakeRepo(failing={"make test"})
    handler, seen = _scripted(_final(), _final())

    result = await _middleware(repo).awrap_model_call(_request(), handler)

    outcome = _outcome(result)
    assert outcome["status"] == "failed"
    assert outcome["rounds"] == 1
    assert repo.check_runs == 1
    assert len(seen) == 2


async def test_finalizing_budget_records_failure_without_reminder():
    repo = FakeRepo(failing={"make test"})
    reply = _final()
    handler, seen = _scripted(reply)

    result = await _middleware(repo, finalizing=True).awrap_model_call(_request(), handler)

    outcome = _outcome(result)
    assert outcome["status"] == "failed"
    assert outcome["rounds"] == 0
    assert result.model_response.result == [reply]
    assert len(seen) == 1


async def test_loop_broken_run_records_failure_without_reminder():
    repo = FakeRepo(failing={"make test"})
    reply = _final("I stopped because I called the same tool repeatedly")
    handler, seen = _scripted(reply)

    result = await _middleware(repo, terminated=True).awrap_model_call(_request(), handler)

    outcome = _outcome(result)
    assert outcome["status"] == "failed"
    assert outcome["rounds"] == 0
    assert result.model_response.result == [reply]
    assert len(seen) == 1


async def test_failed_reminder_call_keeps_the_round_for_the_retry():
    repo = FakeRepo(failing={"make test"})
    middleware = _middleware(repo)
    with pytest.raises(RuntimeError):
        await middleware.awrap_model_call(_request(), _fails_on_reminder)

    handler, seen = _scripted(_final(), _final())
    result = await middleware.awrap_model_call(_request(), handler)

    assert "You have 2 fix round(s) left" in seen[1].messages[-1].content
    outcome = _outcome(result)
    assert outcome["status"] == "failed"
    assert outcome["rounds"] == 1
    assert repo.stashes == 1


async def test_checks_unavailable_records_not_verified():
    repo = FakeRepo(error=httpx.ConnectError("down"))
    reply = _final()
    handler, seen = _scripted(reply)

    result = await _middleware(repo).awrap_model_call(_request(), handler)

    outcome = _outcome(result)
    assert outcome["status"] == "not_verified"
    assert "could not be reached" in outcome["reason"]
    assert result.model_response.result == [reply]
    assert len(seen) == 1


async def test_suspicious_paths_recorded():
    repo = FakeRepo(changed=["tests/test_a.py", "a.py"])
    handler, _ = _scripted(_final())

    result = await _middleware(repo).awrap_model_call(_request(), handler)

    assert _outcome(result)["suspicious_paths"] == ["tests/test_a.py"]


async def test_system_prompt_lists_commands():
    handler, seen = _scripted(_tool_call())

    await _middleware(FakeRepo(), ("make lint", "make test")).awrap_model_call(_request(), handler)

    prompt = seen[0].system_prompt
    assert prompt.startswith("base\n\n")
    assert "- `make lint`\n- `make test`" in prompt
    assert "repository root" in prompt
    assert "exit 0" in prompt


def _edited_history() -> list:
    return [
        HumanMessage(content="task"),
        AIMessage(content="", tool_calls=[{"name": "edit_file", "args": {}, "id": "e1"}]),
        ToolMessage(content="ok", name="edit_file", tool_call_id="e1"),
    ]


async def test_without_commands_reminds_once_when_no_check_ran():
    first_final, second_final = _final("first"), _final("second")
    handler, seen = _scripted(first_final, second_final)

    result = await _middleware(FakeRepo(), ()).awrap_model_call(_request(_edited_history()), handler)

    first, reminder, second = result.model_response.result
    assert (first, second) == (first_final, second_final)
    assert is_synthetic(reminder)
    assert "did not run any test" in reminder.content
    assert len(seen) == 2
    assert _outcome(result) == {"status": "not_configured", "commands": [], "rounds": 0, "suspicious_paths": []}


async def test_without_commands_no_reminder_after_loop_breaker_ended_the_run():
    reply = _final()
    handler, seen = _scripted(reply)

    result = await _middleware(FakeRepo(), (), terminated=True).awrap_model_call(_request(_edited_history()), handler)

    assert result.model_response.result == [reply]
    assert _outcome(result)["status"] == "not_configured"
    assert len(seen) == 1


async def test_without_commands_failed_reminder_call_keeps_the_reminder_for_the_retry():
    middleware = _middleware(FakeRepo(), ())
    with pytest.raises(RuntimeError):
        await middleware.awrap_model_call(_request(_edited_history()), _fails_on_reminder)

    handler, seen = _scripted(_final(), _final())
    await middleware.awrap_model_call(_request(_edited_history()), handler)

    assert "did not run any test" in seen[1].messages[-1].content


async def test_without_commands_no_reminder_after_passing_check():
    history = [
        *_edited_history(),
        AIMessage(content="", tool_calls=[{"name": "bash", "args": {}, "id": "b1"}]),
        ToolMessage(
            content=json.dumps({"commands": [{"command": "pytest", "output": "", "exit_code": 0}]}),
            name="bash",
            tool_call_id="b1",
        ),
    ]
    request = _request(history)
    reply = _final()
    handler, seen = _scripted(reply)

    result = await _middleware(FakeRepo(), ()).awrap_model_call(request, handler)

    assert seen == [request]
    assert result.model_response.result == [reply]
    assert _outcome(result)["status"] == "not_configured"


async def test_abefore_agent_resets_previous_outcome():
    repo = FakeRepo(failing={"make test"})
    middleware = _middleware(repo)
    handler, _ = _scripted(_final(), _final())
    assert _outcome(await middleware.awrap_model_call(_request(), handler))["status"] == "failed"

    assert await middleware.abefore_agent({}, Mock()) == {"verification": None}

    repo.failing = set()
    handler, _ = _scripted(_final())
    outcome = _outcome(await middleware.awrap_model_call(_request(), handler))
    assert outcome["status"] == "passed"
    assert outcome["rounds"] == 0
    assert repo.check_runs == 2
