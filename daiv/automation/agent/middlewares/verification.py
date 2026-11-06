from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, NotRequired, cast

from langchain.agents.middleware import AgentMiddleware, AgentState, ExtendedModelResponse
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from automation.agent.middlewares.reminders import call_with_reminder
from automation.agent.middlewares.sandbox import BASH_TOOL_NAME
from automation.agent.questions import delivered_question_call
from automation.agent.verification.checks import (
    MAX_FIX_ROUNDS,
    UNREADABLE,
    ChecksUnavailableError,
    diff_fingerprint,
    failing_before_change,
    run_checks_in_place,
)
from automation.agent.verification.fallback import fallback_reminder, last_check
from automation.agent.verification.outcome import VerificationOutcome, code_span, command_outcomes, failure_reminder
from automation.agent.verification.suspicious import suspicious_paths
from automation.agent.workspace.git_manager import GIT_ERRORS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain.agents.middleware.types import ModelCallResult, ToolCallRequest
    from langchain_core.messages import AnyMessage, BaseMessage
    from langgraph.runtime import Runtime

    from automation.agent.middlewares.loop_breaker import LoopBreakerMiddleware
    from automation.agent.middlewares.step_budget import StepBudgetMiddleware
    from automation.agent.verification.checks import CheckResult, CheckRun
    from automation.agent.verification.fallback import Check
    from automation.agent.verification.outcome import CommandOutcome, VerificationStatus
    from automation.agent.workspace.base import Workspace
    from automation.agent.workspace.sandbox_backend import SandboxFileBackend

logger = logging.getLogger("daiv.agent")


class VerificationState(AgentState):
    verification: NotRequired[VerificationOutcome | None]


class VerificationResetMiddleware(AgentMiddleware):
    """Start every run with no verification outcome, so a run never reports the previous run's checks.

    Registered ahead of every ``before_agent`` hook that can jump to the end (a slash command), which would skip a
    reset placed later, and whether or not the run is verified, so turning verification off leaves no stale outcome.
    """

    state_schema = VerificationState

    async def abefore_agent(self, state: AgentState, runtime: Runtime[Any]) -> dict[str, Any]:
        return {"verification": None}


def _before_publishing_section(commands: Sequence[str]) -> str:
    listed = "\n".join(f"- {code_span(command)}" for command in commands)
    return (
        "## Before publishing\n\n"
        "Before your changes are published, DAIV runs these commands from the repository root, and each must exit 0:\n"
        f"{listed}\n\n"
        "Run them yourself before you finish and fix what they report."
    )


def _stopped(messages: Sequence[BaseMessage]) -> bool:
    replies = [message for message in messages if isinstance(message, AIMessage)]
    return bool(replies) and not replies[-1].tool_calls


def _outcome(
    status: VerificationStatus,
    *,
    commands: list[CommandOutcome],
    changed: Sequence[str],
    rounds: int,
    fingerprint: str | None = None,
    reason: str | None = None,
    changed_by_checks: Sequence[str] = (),
) -> VerificationOutcome:
    outcome = VerificationOutcome(
        status=status, commands=commands, rounds=rounds, suspicious_paths=suspicious_paths(changed)
    )
    if fingerprint is not None:
        outcome["fingerprint"] = fingerprint
    if reason is not None:
        outcome["reason"] = reason
    if changed_by_checks:
        outcome["changed_by_checks"] = list(changed_by_checks)
    return outcome


@dataclass(frozen=True)
class _AgentCheck:
    """The last check the agent ran through bash, and the fingerprint of the tree it ran on."""

    check: Check
    tree: str


@dataclass(frozen=True)
class _Verdict:
    """What a stop comes to: its outcome and any reminder to send. ``tree`` fingerprints the tree a failure reminder is
    about."""

    outcome: VerificationOutcome
    reminder: str | None = None
    tree: str | None = None


class VerificationMiddleware(AgentMiddleware):
    """
    Check the agent's work when it stops calling tools, before ``GitMiddleware`` publishes it.

    With ``before_publish`` commands, the system prompt lists them, and each time the agent stops with something to
    publish DAIV runs them in the sandbox, once per tree, and then puts back any file they changed. A command that fails
    is re-run once on the starting code; one that failed there too is reported, not sent back, unless its output now
    has lines it did not have there, which the agent is shown without the run counting as failed. The other failures,
    and any file the checks changed, go back to the agent as a reminder, for at most ``MAX_FIX_ROUNDS`` rounds; a stop
    on the tree a reminder was sent for records the failure without another round. Without commands, the agent gets
    one reminder when the tree changed after the last check it ran through bash, or that check failed. No reminder is
    sent once the step budget is finalizing, ``LoopBreakerMiddleware`` has ended the run or the run ends on a question
    ``AskUserQuestionMiddleware`` delivered, which therefore sits inside this one: the outcome is still recorded.

    The outcome is recorded in the ``verification`` state key for the publish and the run executor, and ``recheck``
    judges it again when the tree changed before publishing. Like ``LoopBreakerMiddleware`` this lives in
    ``awrap_model_call``, so it adds no graph node per turn; each reminder is saved into the thread ahead of the reply
    it produced (see ``reminders``).

    A tree that can't be read, or checks that can't run, record the run as not verified. This sits outside
    ``ModelFallbackMiddleware``, so a fallback model retries only the model call, never the checks, and a reminder
    counts only once the call carrying it returned.

    Args:
        workspace: The run's workspace; its ``bash`` runs the checks and its ``git`` reads the tree.
        commands: The ``before_publish`` commands; empty when neither the repository nor its sandbox declares any.
        step_budget: The run's step budget, asked whether a fix round still fits.
        loop_breaker: The run's loop breaker, whose terminal stop must not be reopened.
        redact: Values masked in the checks' output before the agent or the outcome sees it.
    """

    state_schema = VerificationState

    def __init__(
        self,
        *,
        workspace: Workspace,
        commands: tuple[str, ...],
        step_budget: StepBudgetMiddleware,
        loop_breaker: LoopBreakerMiddleware,
        redact: Sequence[str] = (),
    ) -> None:
        super().__init__()
        if workspace.bash is None:
            raise ValueError("VerificationMiddleware needs a workspace with a shell")
        self._workspace = workspace
        self._bash: SandboxFileBackend = workspace.bash
        self._commands = commands
        self._step_budget = step_budget
        self._loop_breaker = loop_breaker
        self._redact = redact
        self._rounds = 0
        self._starting: dict[str, frozenset[str] | None] = {}
        self._runs: dict[str, CheckRun] = {}
        self._reminded_tree: str | None = None
        self._agent_check: _AgentCheck | None = None
        self._fallback_sent = False

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        if self._commands:
            request = request.override(
                system_prompt=f"{request.system_prompt}\n\n{_before_publishing_section(self._commands)}"
            )
        response = await handler(request)
        asked = delivered_question_call(request.messages) is not None
        while _stopped(response.result):
            verdict = await self._verdict(may_remind=not asked and self._may_remind())
            if verdict is None:
                break
            if verdict.reminder is None:
                return ExtendedModelResponse(
                    model_response=response, command=Command(update={"verification": verdict.outcome})
                )
            history = [*request.messages, *cast("list[AnyMessage]", response.result)]
            reply = await call_with_reminder(
                request.override(messages=history), handler, verdict.reminder, kind="verification"
            )
            self._count_reminder(verdict)
            response = replace(reply, result=[*response.result, *reply.result])
        return response

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        # Inside the filesystem middleware, so a bash result too large for the thread is still read whole here.
        result = await handler(request)
        if (
            not self._commands
            and not self._fallback_sent
            and request.tool_call["name"] == BASH_TOOL_NAME
            and isinstance(result, ToolMessage)
        ):
            await self._note_agent_check(result)
        return result

    async def recheck(self, outcome: VerificationOutcome) -> VerificationOutcome:
        """``outcome`` judged again, with no reminder, when the tree changed after its checks ran; ``GitMiddleware``
        calls it before publishing. A tree that can't be read makes it not verified."""
        if outcome["status"] not in ("passed", "failed"):
            return outcome
        git = self._workspace.git
        try:
            before = await git.get_diff()
            if diff_fingerprint(before) == outcome.get("fingerprint"):
                return outcome
            merging = await git.pending_merge() is not None
            verdict = await self._verify(await git.get_changed_files(), before, merging=merging, may_remind=False)
        except GIT_ERRORS:
            logger.exception("Re-checking the tree before publishing failed; publishing it as not verified")
            return VerificationOutcome(
                status="not_verified",
                commands=[],
                rounds=outcome["rounds"],
                suspicious_paths=outcome["suspicious_paths"],
                reason="the tree could not be re-checked after the last checks",
            )
        return verdict.outcome

    def _may_remind(self) -> bool:
        return not self._loop_breaker.terminated and not self._step_budget.is_finalizing()

    def _count_reminder(self, verdict: _Verdict) -> None:
        """Record a reminder the agent got, once the call carrying it returned."""
        if self._commands:
            self._rounds = verdict.outcome["rounds"]
            self._reminded_tree = verdict.tree
        else:
            self._fallback_sent = True

    async def _note_agent_check(self, message: ToolMessage) -> None:
        check = last_check(message)
        if check is None:
            return
        try:
            tree = diff_fingerprint(await self._workspace.git.get_diff())
        except GIT_ERRORS:
            logger.warning("Could not read the repository after the agent ran a check", exc_info=True)
            return
        self._agent_check = _AgentCheck(check, tree)

    async def _verdict(self, *, may_remind: bool) -> _Verdict | None:
        """What the agent's stop comes to, or ``None`` when it leaves nothing to publish."""
        git = self._workspace.git
        try:
            changed = await git.get_changed_files()
            merge = await git.pending_merge() if self._commands or not changed else None
            if not changed and merge is None:
                return None
            before = await git.get_diff()
        except GIT_ERRORS:
            logger.warning("Could not read the repository to verify the agent's work", exc_info=True)
            if not self._commands:
                return _Verdict(_outcome("not_configured", commands=[], changed=[], rounds=0))
            return self._not_verified([], UNREADABLE)
        if self._commands:
            return await self._verify(changed, before, merging=merge is not None, may_remind=may_remind)
        return self._fallback(changed, diff_fingerprint(before), may_remind=may_remind)

    def _not_verified(self, changed: Sequence[str], reason: str) -> _Verdict:
        return _Verdict(_outcome("not_verified", commands=[], changed=changed, rounds=self._rounds, reason=reason))

    async def _verify(self, changed: Sequence[str], before: str, *, merging: bool, may_remind: bool) -> _Verdict:
        tree = diff_fingerprint(before)
        try:
            run = self._runs.get(tree)
            if run is None:
                run = self._runs[tree] = await run_checks_in_place(
                    self._bash, self._workspace.git, self._commands, before=before, merging=merging, redact=self._redact
                )
            baseline = await self._baseline([result for result in run.results if not result.passed], merging=merging)
        except ChecksUnavailableError as exc:
            logger.warning("Could not run the before_publish checks: %s", exc, exc_info=True)
            return self._not_verified(changed, str(exc))

        failures = [result for result in run.results if not result.passed and result.command not in baseline]
        output_changed = [
            result
            for result in run.results
            if result.command in baseline and not result.signature <= baseline[result.command]
        ]
        failed = bool(failures or run.changed_by_checks)
        remind = (
            may_remind
            and (failed or bool(output_changed))
            and tree != self._reminded_tree
            and self._rounds < MAX_FIX_ROUNDS
        )
        outcome = _outcome(
            "failed" if failed else "passed",
            commands=command_outcomes(run.results, baseline, {result.command for result in output_changed}),
            changed=changed,
            fingerprint=run.fingerprint,
            rounds=self._rounds + 1 if remind else self._rounds,
            changed_by_checks=run.changed_by_checks,
        )
        if not remind:
            return _Verdict(outcome)
        reminder = failure_reminder(
            failures, run.changed_by_checks, MAX_FIX_ROUNDS - self._rounds, changed_output=output_changed
        )
        return _Verdict(outcome, reminder, tree)

    async def _baseline(self, failing: Sequence[CheckResult], *, merging: bool) -> dict[str, frozenset[str]]:
        """The ``failing`` commands that also fail on the starting code, each with its output signature there,
        re-checking each command at most once per run."""
        unchecked = [result for result in failing if result.command not in self._starting]
        if unchecked:
            found = await failing_before_change(
                self._bash, self._workspace.git, unchecked, merging=merging, redact=self._redact
            )
            self._starting.update({result.command: found.get(result.command) for result in unchecked})
        return {
            result.command: signature for result in failing if (signature := self._starting[result.command]) is not None
        }

    def _fallback(self, changed: list[str], tree: str, *, may_remind: bool) -> _Verdict:
        """The ``not_configured`` outcome, and the run's one reminder when no passing check ran on this tree."""
        agent_check = self._agent_check
        check = agent_check.check if agent_check is not None and agent_check.tree == tree else None
        reminder = None
        if (check is None or check.exit_code != 0) and not self._fallback_sent and may_remind:
            reminder = fallback_reminder(check, changed)
        return _Verdict(_outcome("not_configured", commands=[], changed=changed, rounds=0), reminder)
