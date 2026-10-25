from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any, NotRequired, cast

from langchain.agents.middleware import AgentMiddleware, AgentState, ExtendedModelResponse
from langchain_core.messages import AIMessage
from langgraph.types import Command

from automation.agent.synthetic import synthetic_message
from automation.agent.verification.checks import (
    MAX_FIX_ROUNDS,
    ChecksUnavailableError,
    failing_before_change,
    run_checks,
)
from automation.agent.verification.fallback import checks_after_last_edit, fallback_reminder
from automation.agent.verification.outcome import VerificationOutcome, command_outcomes, failure_reminder
from automation.agent.verification.suspicious import suspicious_paths

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain.agents.middleware.types import ModelCallResult
    from langchain_core.messages import AnyMessage, BaseMessage
    from langgraph.runtime import Runtime

    from automation.agent.middlewares.step_budget import StepBudgetMiddleware
    from automation.agent.verification.outcome import CommandOutcome, VerificationStatus
    from automation.agent.workspace.base import Workspace
    from automation.agent.workspace.sandbox_backend import SandboxFileBackend

logger = logging.getLogger("daiv.agent")

_DIFF_HEADER = re.compile(r"^diff --git ", re.MULTILINE)


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


def _fingerprint(diff: str) -> str:
    return hashlib.sha256(diff.encode()).hexdigest()


def _section_path(header: str) -> str:
    """The path in a ``a/<path> b/<path>`` header; a rename's header, which names two paths, is kept whole."""
    path = header[2 : 2 + (len(header) - 5) // 2]
    return path if header == f"a/{path} b/{path}" else header


def _diff_sections(diff: str) -> dict[str, str]:
    sections = _DIFF_HEADER.split(diff)[1:]
    return {_section_path(section.partition("\n")[0]): section for section in sections}


def _changed_between(before: str, after: str) -> list[str]:
    old, new = _diff_sections(before), _diff_sections(after)
    return [path for path in dict.fromkeys([*old, *new]) if old.get(path) != new.get(path)]


def _outcome(
    status: VerificationStatus,
    *,
    commands: list[CommandOutcome],
    changed: Sequence[str],
    fingerprint: str,
    rounds: int,
    reason: str | None = None,
) -> VerificationOutcome:
    outcome = VerificationOutcome(
        status=status,
        commands=commands,
        rounds=rounds,
        suspicious_paths=suspicious_paths(changed),
        fingerprint=fingerprint,
    )
    if reason is not None:
        outcome["reason"] = reason
    return outcome


class VerificationMiddleware(AgentMiddleware):
    """
    Check the agent's work when it stops calling tools, before ``GitMiddleware`` publishes it.

    With ``before_publish`` commands, the system prompt lists them, and each time the agent stops with something to
    publish DAIV runs them in the sandbox. A command that fails is re-run once on the starting code; one that already
    failed there is reported, not sent back. The other failures, and any file the checks themselves changed, go back to
    the agent as a reminder, for at most ``MAX_FIX_ROUNDS`` rounds and none once the step budget is finalizing. A tree
    the checks already ran on is not checked again. Without commands, the agent gets one reminder when it edited files
    and ran no check after its last edit, or the last one failed.

    The outcome is recorded in the ``verification`` state key for the publish and the run executor. Like
    ``LoopBreakerMiddleware`` this lives in ``awrap_model_call``, so it adds no graph node per turn; each reminder is
    saved into the thread ahead of the reply it produced (see ``reminders``).

    Args:
        workspace: The run's workspace; its ``bash`` runs the checks and its ``git`` reads the tree.
        commands: The ``before_publish`` commands; empty when the repository declares none.
        step_budget: The run's step budget, asked whether a fix round still fits.
    """

    state_schema = VerificationState

    def __init__(self, *, workspace: Workspace, commands: tuple[str, ...], step_budget: StepBudgetMiddleware) -> None:
        super().__init__()
        if workspace.bash is None:
            raise ValueError("VerificationMiddleware needs a workspace with a shell")
        self._workspace = workspace
        self._bash: SandboxFileBackend = workspace.bash
        self._commands = commands
        self._step_budget = step_budget
        self._reset()

    def _reset(self) -> None:
        self._rounds = 0
        self._fails_on_starting_code: dict[str, bool] = {}
        self._last_outcome: VerificationOutcome | None = None
        self._fallback_sent = False

    async def abefore_agent(self, state: AgentState, runtime: Runtime[Any]) -> dict[str, Any]:
        """Start the run with no outcome, so a follow-up turn never reports the previous turn's checks."""
        self._reset()
        return {"verification": None}

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        if self._commands:
            request = request.override(
                system_prompt=f"{request.system_prompt}\n\n{_before_publishing_section(self._commands)}"
            )
        response = await handler(request)
        while _stopped(response.result):
            git = self._workspace.git
            changed = await git.get_changed_files()
            if not changed and await git.pending_merge() is None:
                break
            history = [*request.messages, *cast("list[AnyMessage]", response.result)]
            if self._commands:
                outcome, reminder = await self._verify(changed)
            else:
                outcome, reminder = self._fallback(history, changed)
            if reminder is None:
                return ExtendedModelResponse(model_response=response, command=Command(update={"verification": outcome}))
            message = synthetic_message(reminder, kind="verification")
            reply = await handler(request.override(messages=[*history, message]))
            response = replace(reply, result=[*response.result, message, *reply.result])
        return response

    async def _verify(self, changed: list[str]) -> tuple[VerificationOutcome, str | None]:
        """The outcome of the checks on the current tree, and the reminder to send when the agent gets another round."""
        git = self._workspace.git
        before = await git.get_diff()
        fingerprint = _fingerprint(before)
        last = self._last_outcome
        if last is not None and last.get("fingerprint") == fingerprint:
            return last, None

        try:
            results = await run_checks(self._bash, self._commands)
            after = await git.get_diff()
            fingerprint = _fingerprint(after)
            changed_by_checks = _changed_between(before, after)
            if changed_by_checks:
                changed = await git.get_changed_files()
            already_failing = await self._already_failing([result.command for result in results if not result.passed])
        except ChecksUnavailableError as exc:
            logger.warning("Could not run the before_publish checks: %s", exc)
            outcome = _outcome(
                "not_verified",
                commands=[],
                changed=changed,
                fingerprint=fingerprint,
                rounds=self._rounds,
                reason=str(exc),
            )
            return outcome, None

        failures = [result for result in results if not result.passed and result.command not in already_failing]
        reminder = None
        if (failures or changed_by_checks) and self._rounds < MAX_FIX_ROUNDS and not self._step_budget.is_finalizing():
            reminder = failure_reminder(failures, changed_by_checks, rounds_left=MAX_FIX_ROUNDS - self._rounds)
            self._rounds += 1
        outcome = _outcome(
            "failed" if failures or changed_by_checks else "passed",
            commands=command_outcomes(results, already_failing),
            changed=changed,
            fingerprint=fingerprint,
            rounds=self._rounds,
        )
        self._last_outcome = outcome
        return outcome, reminder

    async def _already_failing(self, failing: Sequence[str]) -> frozenset[str]:
        """Which of the ``failing`` commands also fail on the starting code, re-checking each at most once per run."""
        unchecked = [command for command in failing if command not in self._fails_on_starting_code]
        if unchecked:
            found = await failing_before_change(self._bash, self._workspace.git, unchecked)
            self._fails_on_starting_code.update({command: command in found for command in unchecked})
        return frozenset(command for command in failing if self._fails_on_starting_code[command])

    def _fallback(self, messages: Sequence[AnyMessage], changed: list[str]) -> tuple[VerificationOutcome, str | None]:
        """The ``not_configured`` outcome, and the run's one reminder when no passing check followed the last edit."""
        reminder = None
        finding = checks_after_last_edit(messages)
        if finding.verdict != "checked" and not self._fallback_sent and not self._step_budget.is_finalizing():
            reminder = fallback_reminder(finding, changed)
            self._fallback_sent = True
        outcome = VerificationOutcome(
            status="not_configured", commands=[], rounds=0, suspicious_paths=suspicious_paths(changed)
        )
        return outcome, reminder
