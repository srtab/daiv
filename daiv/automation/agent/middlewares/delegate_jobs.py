from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.runnables import RunnableConfig  # noqa: TCH002 — used in @tool signature at runtime
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from sandbox_envs.services import aresolve_repo_envs
from sessions.models import MAX_SPAWN_DEPTH, Session, SessionOrigin
from sessions.services import MAX_DELEGATED_TARGETS, RepoTarget, asubmit_batch_runs

from codebase.authorization import RepositoryAccessDenied, aassert_can_run

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger("daiv.tools")

DELEGATE_JOBS_NAME = "delegate_jobs"


class DelegateTarget(BaseModel):
    repo_id: str = Field(description="Identifier of the target repository.")
    ref: str | None = Field(default=None, description="Base branch/ref to start from; omit for the default branch.")
    prompt: str = Field(
        min_length=1,
        description="Self-contained instruction for this repository. The leg runs in an isolated session "
        "and sees only this text — not your conversation, the originating request, or anything else you "
        "can see here — so include all the context it needs. End with the no-change convention: "
        "'if this repository is unaffected, reply saying so and make no changes.'",
    )


DELEGATE_JOBS_DESCRIPTION = f"""\
Delegate tailored sub-jobs to other repositories and return immediately.

Each target runs as an independent single-repo job (own thread, own MR). Your turn ends after
delegating; you will be resumed automatically with a summary of every leg once they all finish.

- `goal`: one-line description of the overall objective (used to title the batch).
- `targets`: 1-{MAX_DELEGATED_TARGETS} entries, each `{{repo_id, ref?, prompt}}` — `prompt` required per target.

Returns JSON:
{{"batch_id", "delegated": [{{repo_id, ref, thread_id, session_url}}], "failed": [{{repo_id, error}}]}}."""


DELEGATE_JOBS_SYSTEM_PROMPT = f"""\
## Delegation tool `{DELEGATE_JOBS_NAME}`

When a task spans other repositories, use `{DELEGATE_JOBS_NAME}` to fan tailored work out to them —
each target runs as an independent job. This is only for work in *other* repositories; for parallel
work inside this one, use the `task` tool (subagents) instead. If the task is contained to this
repository, ignore this tool.
After you call it, state your plan and end your turn — do NOT poll or wait. You will be resumed with
the consolidated results when every delegated leg finishes.
Each leg runs in isolation and sees only the prompt you give it, so make every target's prompt
self-contained — include the context it needs and the no-change convention.
"""


@tool(DELEGATE_JOBS_NAME, description=DELEGATE_JOBS_DESCRIPTION)
async def delegate_jobs_tool(goal: str, targets: list[DelegateTarget], config: RunnableConfig) -> str:
    """Delegate per-repo sub-jobs; returns a JSON string (no state mutation)."""
    thread_id = (config.get("configurable") or {}).get("thread_id")
    if not thread_id:
        return json.dumps({"error": "delegate_jobs is only available inside a checkpointed run."})

    session = await Session.objects.select_related("user").filter(thread_id=thread_id).afirst()
    if session is None:
        return json.dumps({"error": "Could not resolve the current session for delegation."})
    if session.user_id is None:
        return json.dumps({"error": "Delegation requires an authenticated coordinator; this session has no user."})

    if not targets:
        return json.dumps({"error": "At least one target is required."})
    if len(targets) > MAX_DELEGATED_TARGETS:
        return json.dumps({"error": f"At most {MAX_DELEGATED_TARGETS} targets per delegate_jobs call."})
    if session.spawn_depth + 1 > MAX_SPAWN_DEPTH:
        return json.dumps({"error": f"Delegation depth limit reached (MAX_SPAWN_DEPTH={MAX_SPAWN_DEPTH})."})
    coordinator_checkout = (session.repo_id, session.ref or "")
    if any((t.repo_id, t.ref or "") == coordinator_checkout for t in targets):
        # delegate_jobs fans out to independent jobs on *other* checkouts; delegating to the
        # coordinator's own repo+ref would spawn a redundant run on this very checkout. In-repo
        # parallelism belongs to subagents. A different ref on the same repo is a distinct checkout,
        # so it is allowed to delegate normally.
        return json.dumps({
            "error": (
                f"Cannot delegate to the coordinator's own checkout ({session.repo_id!r} on "
                f"{session.ref or 'default branch'!r}). delegate_jobs fans work out to other "
                "checkouts as independent jobs; for parallel work on this one, use the `task` "
                "tool (subagents) instead."
            )
        })

    seen: set[tuple[str, str]] = set()
    for t in targets:
        key = (t.repo_id, t.ref or "")
        if key in seen:
            return json.dumps({"error": f"Duplicate target: {t.repo_id} on {t.ref or 'default branch'}."})
        seen.add(key)

    user = session.user

    # Per-target authorization: aassert_can_run is all-or-nothing, so partition on the denied set.
    denied: set[str] = set()
    try:
        await aassert_can_run(user, [t.repo_id for t in targets])
    except RepositoryAccessDenied as exc:
        denied = set(exc.repo_ids)

    allowed = [t for t in targets if t.repo_id not in denied]
    failed = [{"repo_id": rid, "error": "access denied (WRITE required)"} for rid in sorted(denied)]

    batch_id: str | None = None
    delegated: list[dict] = []
    if allowed:
        repo_targets = [RepoTarget(repo_id=t.repo_id, ref=t.ref or "", prompt=t.prompt) for t in allowed]
        # Env resolution and batch submit hit the DB and re-check authorization; a raise here
        # (OperationalError, a revoked-access RepositoryAccessDenied, a validation ValueError) must
        # surface as the tool's JSON error contract, not an opaque tool-node crash. asubmit_batch_runs
        # is best-effort per repo, so a partial result is impossible once it has returned.
        try:
            repo_targets = await aresolve_repo_envs(user=user, repos=repo_targets, explicit_env_id=None)
            result = await asubmit_batch_runs(
                user=user,
                prompt=goal,
                repos=repo_targets,
                trigger_type=SessionOrigin.DELEGATED_JOB,
                parent_thread_id=thread_id,
                spawn_depth=session.spawn_depth + 1,
            )
        except Exception:  # noqa: BLE001
            logger.exception("delegate_jobs: submission failed for thread=%s", thread_id)
            return json.dumps({"error": "Delegation submission failed; no sub-jobs were started."})
        batch_id = str(result.batch_id)
        delegated = [
            {
                "repo_id": run.repo_id,
                "ref": run.ref,
                "thread_id": str(run.session_id),
                "session_url": f"/dashboard/sessions/{run.session_id}/",
            }
            for run in result.runs
        ]
        failed.extend({"repo_id": f.repo_id, "error": f.error} for f in result.failed)

    return json.dumps({"batch_id": batch_id, "delegated": delegated, "failed": failed}, ensure_ascii=False)


class DelegateJobsMiddleware(AgentMiddleware):
    """Bind the delegate_jobs tool and inject its usage note. Added to the agent when
    ``orchestration.enabled`` is set — on by default; a repo opts out with
    ``orchestration.enabled: false``."""

    def __init__(self) -> None:
        self.tools = [delegate_jobs_tool]

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        system_prompt = (request.system_prompt + "\n\n") if request.system_prompt else ""
        system_prompt += DELEGATE_JOBS_SYSTEM_PROMPT
        return await handler(request.override(system_prompt=system_prompt))
