"""The ``delegate_jobs`` tool: fan tailored sub-jobs out to other repositories and end the turn; the run executor's
``JobDelegator`` starts them and resumes the session once they have all finished.

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

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse

    from automation.agent.delegation import JobDelegator

logger = logging.getLogger("daiv.tools")

NO_CHANGE_CONVENTION = "If this repository is unaffected, reply saying so and make no changes."


class DelegateTarget(BaseModel):
    repo_id: str = Field(min_length=1, description="The target repository's identifier.")
    ref: str | None = Field(default=None, description="Branch to start from; omit for the default branch.")
    prompt: str = Field(
        min_length=1,
        description=(
            "Self-contained instruction for this repository. The leg sees only this text, not your conversation, "
            f"the request or your tool results, so include everything it needs. End with: '{NO_CHANGE_CONVENTION}'"
        ),
    )


DELEGATE_JOBS_DESCRIPTION = f"""\
Delegate tailored sub-jobs to other repositories and return without waiting.

Each target runs as an independent job on its repository (its own session and merge request) and sees only its \
`prompt`. Once every started leg has finished, you are resumed with a summary of all of them.

- `goal`: one line naming the overall objective; it titles the batch and is never sent to a leg.
- `targets`: 1-{MAX_DELEGATED_TARGETS} entries of `{{repo_id, ref?, prompt}}`; omit `ref` for the default branch.

Returns JSON: `{{"batch_id", "started": [{{repo_id, ref, thread_id, session_url}}], \
"failed": [{{repo_id, ref, error}}]}}`."""

DELEGATE_JOBS_SYSTEM_PROMPT = f"""\
## Delegating to other repositories (`{DELEGATE_JOBS_TOOL_NAME}`)

This repository coordinates work across other repositories. When a request needs work in other repositories, \
delegate it with `{DELEGATE_JOBS_TOOL_NAME}` instead of doing it here: each target runs as an independent job with \
its own repository's configuration and opens its own merge request.

1. Decide which repositories the request affects and what each must do. This setup's routing rules live in this \
repository's AGENTS.md.
2. Call `{DELEGATE_JOBS_TOOL_NAME}` once, with one target per repository and a prompt tailored to it. A leg sees only \
its prompt, so put everything it needs in it, and end it with: "{NO_CHANGE_CONVENTION}"
3. If `started` is not empty, state your plan and end your turn. Do not poll or wait: you are resumed automatically, \
with each leg's status, merge request and reply, once they have all finished.
4. If `started` is empty, nothing is running and no resume will come: deal with the failures in this turn.
5. When resumed, check the outcome and report it where the request came from (e.g. a comment on the ticket or issue). \
If follow-up work depends on a leg's result, delegate another batch; you are resumed again.

For parallel work inside this repository, use the `task` tool (subagents); `{DELEGATE_JOBS_TOOL_NAME}` refuses this \
repository's own checkout."""

_NOTHING_STARTED_NOTE = "No sub-job is running and no resume will come: deal with the failures in this turn."
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
                checkout=Checkout(repo_id=repo.repo_id, ref=repo.ref, default_branch=repo.config.default_branch or ""),
                goal=goal.strip(),
                targets=[
                    DelegatedTarget(repo_id=t.repo_id.strip(), ref=(t.ref or "").strip(), prompt=t.prompt.strip())
                    for t in targets
                ],
            )
        except DelegationError as exc:
            return json.dumps({"error": str(exc)})
        payload = asdict(result)
        if not result.started:
            payload["note"] = _NOTHING_STARTED_NOTE
        return json.dumps(payload, ensure_ascii=False)

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        system_prompt = f"{request.system_prompt}\n\n" if request.system_prompt else ""
        return await handler(request.override(system_prompt=system_prompt + DELEGATE_JOBS_SYSTEM_PROMPT))
