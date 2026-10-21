"""The ``delegate_jobs`` tool: fan tailored sub-jobs out, to other repositories or to new sessions on this one, and end
the turn; the run executor's ``JobDelegator`` starts them and resumes the session once they have all finished.

Bound only on repositories that opt in with ``orchestration.enabled`` in ``.daiv.yml``, so its system prompt carries
the coordinator workflow that would otherwise need a skill on every repository.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime  # noqa: TC002
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from automation.agent.context import RuntimeCtx  # noqa: TC001
from automation.agent.delegation import (
    DELEGATE_JOBS_TOOL_NAME,
    MAX_DELEGATED_TARGETS,
    Checkout,
    DelegatedTarget,
    DelegationError,
)
from automation.agent.references import MAX_REFS_PER_SUBMISSION, RefIn

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse

    from automation.agent.delegation import JobDelegator

logger = logging.getLogger("daiv.tools")

NO_CHANGE_CONVENTION = "If there is nothing to change, reply saying so and make no changes."


class DelegateTarget(BaseModel):
    repo_id: str = Field(
        min_length=1, description="The repository the leg works on; this repository's own for a sub-session here."
    )
    ref: str | None = Field(
        default=None,
        description=(
            "Branch the leg starts from and its merge request targets; omit for the default branch, or give your "
            "current branch to build on your work."
        ),
    )
    prompt: str = Field(
        min_length=1,
        description=(
            "Self-contained instruction for this leg. It sees only this text, not your conversation, the request or "
            f"your tool results, so include everything it needs. End with: '{NO_CHANGE_CONVENTION}'"
        ),
    )
    references: list[RefIn] = Field(
        default_factory=list,
        max_length=MAX_REFS_PER_SUBMISSION,
        description=(
            "Issues or tickets to link into the leg's merge request, e.g. the Sentry issue it fixes; relation "
            "`closes` resolves it on merge where the provider supports it."
        ),
    )


DELEGATE_JOBS_DESCRIPTION = f"""\
Delegate tailored sub-jobs, on this repository or on others, and return without waiting.

Each target runs as an independent job in a session of its own, sees only its `prompt`, and publishes to a new \
branch and merge request of its own. Once every started leg has finished, you are resumed with a summary of all of \
them.

- `goal`: one line naming the overall objective; it titles the batch and is never sent to a leg.
- `targets`: 1-{MAX_DELEGATED_TARGETS} entries of `{{repo_id, ref?, prompt, references?}}`. `ref` is the branch \
the leg starts from and its merge request targets: omit it for the default branch, or give your current branch to \
build on your work, and that leg starts once your turn ends. `references` link issues or tickets into the leg's \
merge request.

Returns JSON: `{{"batch_id", "started": [{{repo_id, ref, thread_id, session_url, starts_after_this_turn}}], \
"failed": [{{repo_id, ref, error, still_runs}}]}}`. A failed leg with `still_runs` started but cannot be followed: \
it may still open a merge request, so never redo its work."""

DELEGATE_JOBS_SYSTEM_PROMPT = f"""\
## Delegating sub-jobs (`{DELEGATE_JOBS_TOOL_NAME}`)

`{DELEGATE_JOBS_TOOL_NAME}` starts independent jobs (legs), on this repository or on others, and resumes you once \
they have all finished. Each leg runs in a session of its own, with a fresh clone, its own context and budget and \
its repository's configuration, and publishes to a new branch and merge request of its own.

Pick the tool by the size and the destination of the work:
- `task` (subagents): focused or parallel work that fits in this turn and this workspace.
- `{DELEGATE_JOBS_TOOL_NAME}` on this repository: work too big for this turn, or a separate change that deserves a \
merge request of its own, such as one fix per finding of a triage.
- `{DELEGATE_JOBS_TOOL_NAME}` on other repositories: work that belongs there.

Delegate what the request calls for. For a problem you came across on the way, propose the fix first and delegate \
it once the user agrees.

A leg's `ref` decides where its merge request lands. Omit it for the default branch, for a change that stands on its \
own. Give your current branch, even the default one, to build on your work: that leg starts once your turn ends, \
from the branch this turn publishes to, and when you are resumed you can merge its branch into yours or leave its \
merge request for review.

1. Decide what each leg must do. Any routing rules for this setup live in AGENTS.md.
2. Call `{DELEGATE_JOBS_TOOL_NAME}` once, with a prompt tailored to each leg. A leg sees only its prompt, so put \
everything it needs in it, and end it with: "{NO_CHANGE_CONVENTION}" Link the issue or ticket a leg works on with its \
`references`.
3. If `started` is not empty, state your plan and end your turn. Do not poll or wait: you are resumed automatically, \
with each leg's status, branch, merge request and reply, once they have all finished.
4. If `started` is empty, no resume will come: deal with the failures in this turn. Never redo the work of a \
failed leg with `still_runs`; it runs anyway.
5. When resumed, check the outcome and report it where the request came from. If follow-up work depends on a leg's \
result, delegate another batch; you are resumed again."""

_NOTHING_STARTED_NOTE = "No sub-job is running and no resume will come: deal with the failures in this turn."
_UNTRACKED_NOTE = (
    "No resume will come. A leg with `still_runs` runs anyway and may open a merge request: never redo its work. "
    "Deal with the other failures in this turn."
)
_INTERNAL_FAILURE = (
    "Delegation failed on DAIV's side (not a problem with the request). Do not retry; report it in your final response."
)


class DelegateJobsMiddleware(AgentMiddleware):
    """Adds ``delegate_jobs``, which hands its targets to ``delegator``, and the coordinator workflow prompt."""

    def __init__(self, *, delegator: JobDelegator) -> None:
        self._delegator = delegator
        self.tools = [self._build_tool()]

    def _build_tool(self) -> BaseTool:
        @tool(DELEGATE_JOBS_TOOL_NAME, description=DELEGATE_JOBS_DESCRIPTION)
        async def delegate_jobs_tool(goal: str, targets: list[DelegateTarget], runtime: ToolRuntime[RuntimeCtx]) -> str:
            """Start one independent job per target and return the batch it made."""
            try:
                return await self._adelegate(goal, targets, runtime)
            except Exception:
                logger.exception("delegate_jobs: unexpected failure")
                return json.dumps({"error": _INTERNAL_FAILURE})

        return delegate_jobs_tool

    async def _adelegate(self, goal: str, targets: list[DelegateTarget], runtime: ToolRuntime[RuntimeCtx]) -> str:
        repo = runtime.context.repo
        try:
            result = await self._delegator.adelegate(
                checkout=Checkout(repo_id=repo.repo_id, ref=repo.ref),
                goal=goal.strip(),
                targets=[
                    DelegatedTarget(
                        repo_id=t.repo_id.strip(),
                        ref=(t.ref or "").strip(),
                        prompt=t.prompt.strip(),
                        references=tuple(t.references),
                    )
                    for t in targets
                ],
            )
        except DelegationError as exc:
            return json.dumps({"error": str(exc)})
        payload = asdict(result)
        if not result.started:
            payload["note"] = _UNTRACKED_NOTE if any(leg.still_runs for leg in result.failed) else _NOTHING_STARTED_NOTE
        return json.dumps(payload, ensure_ascii=False)

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        system_prompt = f"{request.system_prompt}\n\n" if request.system_prompt else ""
        return await handler(request.override(system_prompt=system_prompt + DELEGATE_JOBS_SYSTEM_PROMPT))
