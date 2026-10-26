from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import Mock

import httpx
import pytest
from git import GitCommandError
from langchain.agents.middleware import ExtendedModelResponse, ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from automation.agent.constants import REPO_PATH
from automation.agent.middlewares.loop_breaker import LoopBreakerMiddleware
from automation.agent.middlewares.step_budget import StepBudgetMiddleware
from automation.agent.middlewares.verification import VerificationMiddleware
from automation.agent.synthetic import is_synthetic
from automation.agent.verification.checks import diff_fingerprint
from automation.agent.workspace.git_manager import StashRestoreError
from automation.agent.workspace.git_runners import SandboxGitProtocolError
from core.sandbox.schemas import RunCommandResult, RunCommandsResponse
from tests.unit_tests.conftest import FakeWorkspace

AGENT_DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
LOCK_DIFF = "diff --git a/uv.lock b/uv.lock\n--- a/uv.lock\n+++ b/uv.lock\n@@ -1 +1 @@\n-v1\n+v2\n"


@dataclass
class FakeRepo:
    """The run's sandbox as both ``workspace.git`` and ``workspace.bash``: ``diff`` is the working tree, ``failing``
    the commands that fail on it, ``failing_on_start`` those that fail on the starting code (while stashed), ``outputs``
    and ``start_outputs`` what a command prints on each, ``checks_leave`` the tree a check run leaves behind,
    ``unreadable_after_checks`` whether the tree can't be read once they ran, ``save_error``, ``stash_error`` and
    ``restore_error`` what saving the tree, stashing and popping raise, and ``read_errors`` what each read of the tree
    raises."""

    changed: list[str] = field(default_factory=lambda: ["a.py"])
    diff: str = AGENT_DIFF
    failing: set[str] = field(default_factory=set)
    failing_on_start: set[str] = field(default_factory=set)
    outputs: dict[str, str] = field(default_factory=dict)
    start_outputs: dict[str, str] = field(default_factory=dict)
    checks_leave: str | None = None
    unreadable_after_checks: bool = False
    error: Exception | None = None
    restore_error: Exception | None = None
    save_error: Exception | None = None
    stash_error: Exception | None = None
    read_errors: dict[str, Exception] = field(default_factory=dict)
    stashed: bool = False
    saved: str | None = None
    stashes: int = 0
    check_runs: int = 0
    diff_reads: int = 0

    def _read(self, method: str) -> None:
        if (error := self.read_errors.get(method)) is not None:
            raise error

    async def get_changed_files(self) -> list[str]:
        self._read("get_changed_files")
        return list(self.changed)

    async def get_diff(self) -> str:
        self._read("get_diff")
        self.diff_reads += 1
        return self.diff

    async def pending_merge(self) -> None:
        self._read("pending_merge")
        return None

    async def stash_all(self, message: str) -> bool:
        if self.stash_error is not None:
            raise self.stash_error
        self.stashes += 1
        self.stashed = True
        return True

    async def save_tree(self, message: str) -> bool:
        if self.save_error is not None:
            raise self.save_error
        self.saved = self.diff
        return True

    async def drop_stash(self) -> None:
        self.saved = None

    async def restore_stash(self) -> None:
        if self.restore_error is not None:
            raise self.restore_error
        if self.stashed:
            self.stashed = False
        elif self.saved is not None:
            self.diff, self.saved = self.saved, None

    async def run_commands(self, commands, *, fail_fast, timeout=None, http_timeout=None) -> RunCommandsResponse:
        if self.error is not None:
            raise self.error
        declared = [command.removeprefix(f"cd {REPO_PATH} && ") for command in commands]
        failing, outputs = (self.failing_on_start, self.start_outputs) if self.stashed else (self.failing, self.outputs)
        if not self.stashed:
            self.check_runs += 1
            if self.checks_leave is not None:
                self.diff = self.checks_leave
            if self.unreadable_after_checks:
                self.read_errors["get_diff"] = httpx.ReadTimeout("timed out")
        return RunCommandsResponse(
            results=[
                RunCommandResult(
                    command=command, output=outputs.get(name, f"{name} output"), exit_code=1 if name in failing else 0
                )
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
    repo: FakeRepo,
    commands: tuple[str, ...] = ("make test",),
    *,
    finalizing: bool = False,
    terminated: bool = False,
    redact: tuple[str, ...] = (),
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
        redact=redact,
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


async def test_check_output_is_masked_in_the_reminder_and_the_outcome():
    repo = FakeRepo(failing={"make test"})
    handler, seen = _scripted(_final(), _final())

    result = await _middleware(repo, redact=("make test output",)).awrap_model_call(_request(), handler)

    reminder = seen[1].messages[-1].content
    assert "******" in reminder
    assert "make test output" not in reminder
    assert _outcome(result)["commands"][0]["output_tail"] == "******"


async def test_failure_already_present_on_starting_code_is_not_sent_back():
    repo = FakeRepo(failing={"make test"}, failing_on_start={"make test"})
    handler, seen = _scripted(_final())

    result = await _middleware(repo).awrap_model_call(_request(), handler)

    outcome = _outcome(result)
    assert outcome["status"] == "passed"
    assert outcome["commands"][0]["already_failing"]
    assert len(seen) == 1


async def test_already_failing_check_with_new_output_is_shown_to_the_agent_without_failing_the_run():
    repo = FakeRepo(
        failing={"make test"},
        failing_on_start={"make test"},
        outputs={"make test": "FAILED test_old\nFAILED test_new\n2 failed in 0.4s"},
        start_outputs={"make test": "FAILED test_old\n1 failed in 0.3s"},
    )
    handler, seen = _scripted(_final(), _final())

    result = await _middleware(repo).awrap_model_call(_request(), handler)

    reminder = seen[1].messages[-1].content
    assert "already failed on the code before your change, but their output is different now" in reminder
    assert "FAILED test_new" in reminder
    outcome = _outcome(result)
    assert outcome["status"] == "passed"
    assert outcome["rounds"] == 1
    assert outcome["commands"][0]["already_failing"]
    assert outcome["commands"][0]["output_changed"]


async def test_already_failing_check_whose_output_only_changed_in_numbers_is_not_sent_back():
    repo = FakeRepo(
        failing={"make test"},
        failing_on_start={"make test"},
        outputs={"make test": "FAILED test_old at 0x7f12\n1 failed in 0.4s"},
        start_outputs={"make test": "FAILED test_old at 0x9a\n1 failed in 12.3s"},
    )
    handler, seen = _scripted(_final())

    outcome = _outcome(await _middleware(repo).awrap_model_call(_request(), handler))

    assert len(seen) == 1
    assert outcome["status"] == "passed"
    assert "output_changed" not in outcome["commands"][0]


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


async def test_check_that_changes_files_counts_as_failed_and_the_files_are_put_back():
    repo = FakeRepo(checks_leave=AGENT_DIFF + LOCK_DIFF)
    handler, _ = _scripted(_final())

    result = await _middleware(repo, finalizing=True).awrap_model_call(_request(), handler)

    outcome = _outcome(result)
    assert outcome["status"] == "failed"
    assert outcome["changed_by_checks"] == ["uv.lock"]
    assert [command["exit_code"] for command in outcome["commands"]] == [0]
    assert repo.diff == AGENT_DIFF
    assert outcome["fingerprint"] == diff_fingerprint(AGENT_DIFF)


async def test_files_the_checks_changed_are_sent_back_once():
    repo = FakeRepo(checks_leave=AGENT_DIFF + LOCK_DIFF)
    handler, seen = _scripted(_final(), _final())

    result = await _middleware(repo).awrap_model_call(_request(), handler)

    assert "DAIV put them back as you left them: uv.lock." in seen[1].messages[-1].content
    outcome = _outcome(result)
    assert outcome["status"] == "failed"
    assert outcome["rounds"] == 1
    assert repo.check_runs == 1


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
    assert repo.check_runs == 1
    assert repo.stashes == 1


async def test_verification_error_is_raised_again_on_later_calls():
    repo = FakeRepo(failing={"make test"}, restore_error=StashRestoreError())
    middleware = _middleware(repo)
    handler, seen = _scripted(_final())
    with pytest.raises(StashRestoreError) as first:
        await middleware.awrap_model_call(_request(), handler)

    with pytest.raises(StashRestoreError) as again:
        await middleware.awrap_model_call(_request(), handler)

    assert again.value is first.value
    assert len(seen) == 1


async def test_checks_unavailable_records_not_verified():
    repo = FakeRepo(error=httpx.ConnectError("All connection attempts failed: http://sandbox:8000/session/s-1/"))
    reply = _final()
    handler, seen = _scripted(reply)

    result = await _middleware(repo).awrap_model_call(_request(), handler)

    outcome = _outcome(result)
    assert outcome["status"] == "not_verified"
    assert outcome["reason"] == "the sandbox could not be reached to run the checks"
    assert result.model_response.result == [reply]
    assert len(seen) == 1


_READ_ERRORS = [
    httpx.ConnectError("All connection attempts failed: http://sandbox:8000/session/s-1/"),
    SandboxGitProtocolError("Sandbox returned no result for: git diff"),
    GitCommandError(["git", "diff"], 128, "fatal: unable to read tree"),
]


@pytest.mark.parametrize("method", ["get_changed_files", "pending_merge", "get_diff"])
@pytest.mark.parametrize("error", _READ_ERRORS, ids=type)
async def test_unreadable_tree_records_not_verified_and_the_run_goes_on(method, error):
    repo = FakeRepo(changed=[] if method == "pending_merge" else ["a.py"], read_errors={method: error})
    middleware = _middleware(repo)
    handler, _ = _scripted(_final(), _final())

    outcome = _outcome(await middleware.awrap_model_call(_request(), handler))

    assert outcome["status"] == "not_verified"
    assert outcome["reason"] == "the repository could not be read to run the checks"
    assert repo.check_runs == 0
    repo.read_errors, repo.changed = {}, ["a.py"]
    assert _outcome(await middleware.awrap_model_call(_request(), handler))["status"] == "passed"


async def test_tree_unreadable_after_the_checks_records_not_verified():
    repo = FakeRepo(failing={"make test"}, unreadable_after_checks=True)
    handler, seen = _scripted(_final())

    outcome = _outcome(await _middleware(repo).awrap_model_call(_request(), handler))

    assert outcome["status"] == "not_verified"
    assert outcome["reason"] == "the repository could not be read to run the checks"
    assert repo.check_runs == 1
    assert repo.saved is None
    assert len(seen) == 1


async def test_tree_that_cannot_be_saved_records_not_verified():
    repo = FakeRepo(save_error=GitCommandError(["git", "stash", "push"], 1, "cannot lock ref"))
    handler, _ = _scripted(_final())

    outcome = _outcome(await _middleware(repo).awrap_model_call(_request(), handler))

    assert outcome["status"] == "not_verified"
    assert outcome["reason"] == "could not set the changes aside to run the checks"
    assert repo.check_runs == 0


async def test_without_commands_an_unreadable_tree_records_not_configured_without_a_reminder():
    repo = FakeRepo(read_errors={"get_changed_files": httpx.ConnectError("down")})
    reply = _final()
    handler, seen = _scripted(reply)

    result = await _middleware(repo, ()).awrap_model_call(_request(), handler)

    assert _outcome(result)["status"] == "not_configured"
    assert result.model_response.result == [reply]
    assert len(seen) == 1


async def test_starting_code_that_cannot_be_rechecked_records_not_verified():
    repo = FakeRepo(failing={"make test"}, stash_error=GitCommandError(["git", "stash"], 1, "cannot lock ref"))
    middleware = _middleware(repo)
    handler, seen = _scripted(_final(), _final(), _final())

    outcome = _outcome(await middleware.awrap_model_call(_request(), handler))

    assert outcome["status"] == "not_verified"
    assert outcome["reason"] == "could not set the changes aside to re-check the starting code"
    assert len(seen) == 1
    repo.stash_error = None
    assert _outcome(await middleware.awrap_model_call(_request(), handler))["status"] == "failed"


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


def _bash_message(command: str, exit_code: int) -> ToolMessage:
    content = json.dumps({"commands": [{"command": command, "output": "", "exit_code": exit_code}]})
    return ToolMessage(content=content, name="bash", tool_call_id="b1")


async def _agent_runs(middleware: VerificationMiddleware, message: ToolMessage, tool: str = "bash"):
    request = Mock(tool_call={"name": tool, "args": {}, "id": "b1"})

    async def handler(_request):
        return message

    return await middleware.awrap_tool_call(request, handler)


async def test_without_commands_reminds_once_when_no_check_ran():
    first_final, second_final = _final("first"), _final("second")
    handler, seen = _scripted(first_final, second_final)

    result = await _middleware(FakeRepo(), ()).awrap_model_call(_request(), handler)

    first, reminder, second = result.model_response.result
    assert (first, second) == (first_final, second_final)
    assert is_synthetic(reminder)
    assert "did not run any test" in reminder.content
    assert len(seen) == 2
    assert _outcome(result) == {"status": "not_configured", "commands": [], "rounds": 0, "suspicious_paths": []}


async def test_without_commands_no_reminder_after_loop_breaker_ended_the_run():
    reply = _final()
    handler, seen = _scripted(reply)

    result = await _middleware(FakeRepo(), (), terminated=True).awrap_model_call(_request(), handler)

    assert result.model_response.result == [reply]
    assert _outcome(result)["status"] == "not_configured"
    assert len(seen) == 1


async def test_without_commands_failed_reminder_call_keeps_the_reminder_for_the_retry():
    middleware = _middleware(FakeRepo(), ())
    with pytest.raises(RuntimeError):
        await middleware.awrap_model_call(_request(), _fails_on_reminder)

    handler, seen = _scripted(_final(), _final())
    await middleware.awrap_model_call(_request(), handler)

    assert "did not run any test" in seen[1].messages[-1].content


async def test_without_commands_no_reminder_after_a_passing_check_on_this_tree():
    middleware = _middleware(FakeRepo(), ())
    message = _bash_message("uv run pytest", 0)
    assert await _agent_runs(middleware, message) is message
    reply = _final()
    handler, seen = _scripted(reply)

    result = await middleware.awrap_model_call(_request(), handler)

    assert len(seen) == 1
    assert result.model_response.result == [reply]
    assert _outcome(result)["status"] == "not_configured"


async def test_without_commands_reminds_when_the_tree_changed_after_the_last_check():
    repo = FakeRepo()
    middleware = _middleware(repo, ())
    await _agent_runs(middleware, _bash_message("pytest", 0))
    repo.diff += "+sed -i edit\n"
    handler, seen = _scripted(_final(), _final())

    await middleware.awrap_model_call(_request(), handler)

    assert "did not run any test" in seen[1].messages[-1].content


async def test_without_commands_reminds_when_the_last_check_failed():
    middleware = _middleware(FakeRepo(), ())
    await _agent_runs(middleware, _bash_message("pytest", 0))
    await _agent_runs(middleware, _bash_message("ruff check .", 1))
    handler, seen = _scripted(_final(), _final())

    await middleware.awrap_model_call(_request(), handler)

    assert "`ruff check .`, failed with exit code 1" in seen[1].messages[-1].content


@pytest.mark.parametrize(
    ("tool", "command"), [("bash", "ls -la"), ("read_file", "pytest")], ids=["not a check", "not bash"]
)
async def test_without_commands_only_bash_checks_count(tool, command):
    repo = FakeRepo()
    middleware = _middleware(repo, ())
    await _agent_runs(middleware, _bash_message(command, 0), tool=tool)
    assert repo.diff_reads == 0
    handler, seen = _scripted(_final(), _final())

    await middleware.awrap_model_call(_request(), handler)

    assert "did not run any test" in seen[1].messages[-1].content


async def test_with_commands_the_agents_checks_are_not_tracked():
    repo = FakeRepo()
    await _agent_runs(_middleware(repo), _bash_message("pytest", 0))

    assert repo.diff_reads == 0


async def test_without_commands_the_agents_checks_are_not_tracked_after_the_reminder():
    repo = FakeRepo()
    middleware = _middleware(repo, ())
    handler, _ = _scripted(_final(), _final())
    await middleware.awrap_model_call(_request(), handler)
    reads = repo.diff_reads

    await _agent_runs(middleware, _bash_message("pytest", 0))

    assert repo.diff_reads == reads


async def test_a_command_result_from_a_tool_is_passed_through():
    command = Command(update={})

    assert await _agent_runs(_middleware(FakeRepo(), ()), command) is command


def _recorded(middleware: VerificationMiddleware):
    handler, _ = _scripted(_final())
    return middleware.awrap_model_call(_request(), handler)


async def test_recheck_keeps_the_outcome_when_the_tree_is_unchanged():
    repo = FakeRepo()
    middleware = _middleware(repo)
    outcome = _outcome(await _recorded(middleware))

    assert await middleware.recheck(outcome) is outcome
    assert repo.check_runs == 1


async def test_recheck_judges_a_changed_tree_again_without_a_reminder():
    repo = FakeRepo()
    middleware = _middleware(repo, redact=("make test output",))
    outcome = _outcome(await _recorded(middleware))
    repo.diff += "+late change\n"
    repo.failing = {"make test"}

    updated = await middleware.recheck(outcome)

    assert updated["status"] == "failed"
    assert updated["rounds"] == 0
    assert updated["fingerprint"] == diff_fingerprint(repo.diff)
    assert updated["commands"][0]["output_tail"] == "******"
    assert repo.check_runs == 2


async def test_recheck_leaves_outcomes_that_ran_no_checks_alone():
    middleware = _middleware(FakeRepo())
    for status in ("not_verified", "not_configured"):
        outcome = {"status": status, "commands": [], "rounds": 0, "suspicious_paths": [], "fingerprint": "x"}
        assert await middleware.recheck(outcome) is outcome


async def test_recheck_records_not_verified_when_the_tree_cannot_be_read():
    repo = FakeRepo(changed=["tests/test_a.py"])
    middleware = _middleware(repo)
    outcome = _outcome(await _recorded(middleware))
    repo.read_errors = {"get_diff": httpx.ConnectError("down")}

    updated = await middleware.recheck(outcome)

    assert updated["status"] == "not_verified"
    assert updated["reason"] == "the tree could not be re-checked after the last checks"
    assert updated["rounds"] == 0
    assert updated["suspicious_paths"] == ["tests/test_a.py"]


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
