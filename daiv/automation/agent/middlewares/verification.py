from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, NotRequired, cast

import httpx
from git import GitCommandError
from langchain.agents.middleware import AgentMiddleware, AgentState, ExtendedModelResponse
from langchain_core.messages import AIMessage
from langgraph.types import Command

from automation.agent.synthetic import synthetic_message
from automation.agent.verification.checks import (
    MAX_FIX_ROUNDS,
    ChecksUnavailableError,
    changed_between,
    diff_fingerprint,
    failing_before_change,
    run_checks,
)
from automation.agent.verification.fallback import checks_after_last_edit, fallback_reminder
from automation.agent.verification.outcome import VerificationOutcome, command_outcomes, failure_reminder
from automation.agent.verification.suspicious import suspicious_paths
from automation.agent.workspace.git_runners import SandboxGitProtocolError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain.agents.middleware.types import ModelCallResult
    from langchain_core.messages import AnyMessage, BaseMessage
    from langgraph.runtime import Runtime

    from automation.agent.middlewares.loop_breaker import LoopBreakerMiddleware
    from automation.agent.middlewares.step_budget import StepBudgetMiddleware
    from automation.agent.verification.checks import CheckResult
    from automation.agent.verification.outcome import CommandOutcome, VerificationStatus
    from automation.agent.workspace.base import Workspace
    from automation.agent.workspace.sandbox_backend import SandboxFileBackend

logger = logging.getLogger("daiv.agent")

_READ_ERRORS = (GitCommandError, httpx.HTTPError, SandboxGitProtocolError)
_UNREADABLE = "the repository could not be read to run the checks"


class VerificationState(AgentState):
    verification: NotRequired[VerificationOutcome | None]


def _before_publishing_section(commands: Sequence[str]) -> str:
    listed = "\n".join(f"- `{command}`" for command in commands)
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
class _CheckRun:
    """One run of the checks on a tree: its results, the files it changed, the repository's changed files after it, and
    the fingerprint of the tree it left."""

    results: list[CheckResult]
    changed_by_checks: list[str]
    changed: list[str]
    fingerprint: str


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
    publish DAIV runs them in the sandbox, once per tree. A command that fails is re-run once on the starting code; one
    that already failed there is reported, not sent back. The other failures, and any file the checks themselves
    changed, go back to the agent as a reminder, for at most ``MAX_FIX_ROUNDS`` rounds; a stop on the tree a reminder
    was sent for records the failure without another round. Without commands, the agent gets one reminder when it
    edited files and ran no check after its last edit, or the last one failed. No reminder is sent once the step budget
    is finalizing or ``LoopBreakerMiddleware`` has ended the run: the outcome is still recorded.

    The outcome is recorded in the ``verification`` state key for the publish and the run executor. Like
    ``LoopBreakerMiddleware`` this lives in ``awrap_model_call``, so it adds no graph node per turn; each reminder is
    saved into the thread ahead of the reply it produced (see ``reminders``).

    A tree that can't be read, or checks that can't run, record the run as not verified. ``ModelFallbackMiddleware``
    retries the whole chain on any error. So a reminder counts only once the call carrying it returned, and any other
    error from the verification itself is raised again on every later call of the run: a retry after a failed stash
    pop would find a clean tree and publish nothing.

    Args:
        workspace: The run's workspace; its ``bash`` runs the checks and its ``git`` reads the tree.
        commands: The ``before_publish`` commands; empty when the repository declares none.
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
        self._reset()

    def _reset(self) -> None:
        self._rounds = 0
        self._fails_on_starting_code: dict[str, bool] = {}
        self._runs: dict[str, _CheckRun] = {}
        self._reminded_tree: str | None = None
        self._fallback_sent = False
        self._error: Exception | None = None

    async def abefore_agent(self, state: AgentState, runtime: Runtime[Any]) -> dict[str, Any]:
        """Start the run with no outcome, so a follow-up turn never reports the previous turn's checks."""
        self._reset()
        return {"verification": None}

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        if self._error is not None:
            raise self._error
        if self._commands:
            request = request.override(
                system_prompt=f"{request.system_prompt}\n\n{_before_publishing_section(self._commands)}"
            )
        response = await handler(request)
        while _stopped(response.result):
            history = [*request.messages, *cast("list[AnyMessage]", response.result)]
            try:
                verdict = await self._verdict(history)
            except Exception as exc:
                self._error = exc
                raise
            if verdict is None:
                break
            if verdict.reminder is None:
                return ExtendedModelResponse(
                    model_response=response, command=Command(update={"verification": verdict.outcome})
                )
            message = synthetic_message(verdict.reminder, kind="verification")
            reply = await handler(request.override(messages=[*history, message]))
            self._count_reminder(verdict)
            response = replace(reply, result=[*response.result, message, *reply.result])
        return response

    def _may_remind(self) -> bool:
        return not self._loop_breaker.terminated and not self._step_budget.is_finalizing()

    def _count_reminder(self, verdict: _Verdict) -> None:
        """Record a reminder the agent got, once the call carrying it returned."""
        if self._commands:
            self._rounds = verdict.outcome["rounds"]
            self._reminded_tree = verdict.tree
        else:
            self._fallback_sent = True

    async def _verdict(self, history: Sequence[AnyMessage]) -> _Verdict | None:
        """What the agent's stop comes to, or ``None`` when it leaves nothing to publish."""
        git = self._workspace.git
        try:
            changed = await git.get_changed_files()
            if not changed and await git.pending_merge() is None:
                return None
        except _READ_ERRORS:
            logger.warning("Could not read the repository to verify the agent's work", exc_info=True)
            return self._unreadable([])
        if self._commands:
            return await self._verify(changed)
        return self._fallback(history, changed)

    def _unreadable(self, changed: Sequence[str]) -> _Verdict:
        if not self._commands:
            return _Verdict(_outcome("not_configured", commands=[], changed=changed, rounds=0))
        return self._not_verified(changed, _UNREADABLE)

    def _not_verified(self, changed: Sequence[str], reason: str) -> _Verdict:
        return _Verdict(_outcome("not_verified", commands=[], changed=changed, rounds=self._rounds, reason=reason))

    async def _verify(self, changed: list[str]) -> _Verdict:
        try:
            before = await self._workspace.git.get_diff()
            tree = diff_fingerprint(before)
            run = self._runs.get(tree)
            if run is None:
                run = await self._run_checks(before, changed)
                self._runs[tree] = run
        except _READ_ERRORS:
            logger.warning("Could not read the repository around the before_publish checks", exc_info=True)
            return self._unreadable(changed)
        except ChecksUnavailableError as exc:
            logger.warning("Could not run the before_publish checks: %s", exc, exc_info=True)
            return self._not_verified(changed, str(exc))
        try:
            already_failing = await self._already_failing([
                result.command for result in run.results if not result.passed
            ])
        except ChecksUnavailableError as exc:
            logger.warning("Could not re-check the starting code: %s", exc, exc_info=True)
            return self._not_verified(changed, str(exc))

        failures = [result for result in run.results if not result.passed and result.command not in already_failing]
        failed = bool(failures or run.changed_by_checks)
        remind = failed and tree != self._reminded_tree and self._rounds < MAX_FIX_ROUNDS and self._may_remind()
        outcome = _outcome(
            "failed" if failed else "passed",
            commands=command_outcomes(run.results, already_failing),
            changed=run.changed,
            fingerprint=run.fingerprint,
            rounds=self._rounds + 1 if remind else self._rounds,
            changed_by_checks=run.changed_by_checks,
        )
        if not remind:
            return _Verdict(outcome)
        reminder = failure_reminder(failures, run.changed_by_checks, rounds_left=MAX_FIX_ROUNDS - self._rounds)
        return _Verdict(outcome, reminder, tree)

    async def _run_checks(self, before: str, changed: list[str]) -> _CheckRun:
        git = self._workspace.git
        results = await run_checks(self._bash, self._commands, redact=self._redact)
        after = await git.get_diff()
        changed_by_checks = changed_between(before, after)
        if changed_by_checks:
            changed = await git.get_changed_files()
        return _CheckRun(results, changed_by_checks, changed, diff_fingerprint(after))

    async def _already_failing(self, failing: Sequence[str]) -> frozenset[str]:
        """Which of the ``failing`` commands also fail on the starting code, re-checking each at most once per run."""
        unchecked = [command for command in failing if command not in self._fails_on_starting_code]
        if unchecked:
            found = await failing_before_change(self._bash, self._workspace.git, unchecked)
            self._fails_on_starting_code.update({command: command in found for command in unchecked})
        return frozenset(command for command in failing if self._fails_on_starting_code[command])

    def _fallback(self, messages: Sequence[AnyMessage], changed: list[str]) -> _Verdict:
        """The ``not_configured`` outcome, and the run's one reminder when no passing check followed the last edit."""
        reminder = None
        finding = checks_after_last_edit(messages)
        if finding.verdict != "checked" and not self._fallback_sent and self._may_remind():
            reminder = fallback_reminder(finding, changed)
        return _Verdict(_outcome("not_configured", commands=[], changed=changed, rounds=0), reminder)
