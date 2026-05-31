from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any, cast

import httpx
from asgiref.sync import sync_to_async
from github import GithubException
from gitlab.exceptions import GitlabError
from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import PrivateStateAttr, hook_config
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import SystemMessagePromptTemplate
from langsmith import get_current_run_tree

from automation.agent.git_utils import open_git_manager
from automation.agent.publishers import GitChangePublisher
from automation.agent.tools.git_publish import commit_changes, create_merge_request
from codebase.base import MergeRequest, Scope
from codebase.clients import RepoClient
from codebase.context import RuntimeCtx  # noqa: TC001
from codebase.utils import get_repo_ref

# Platform / transport errors that warrant a soft "no MR" fallback. Bugs
# (KeyError, AttributeError, etc.) propagate so the run fails loudly rather
# than producing a duplicate MR downstream.
_MR_LOOKUP_PLATFORM_ERRORS: tuple[type[BaseException], ...] = (GitlabError, GithubException, httpx.HTTPError)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langgraph.runtime import Runtime


logger = logging.getLogger("daiv.tools")

# Bounded number of times the safeguard re-prompts the agent to publish its own work
# before daiv publishes it directly.
MAX_GIT_NUDGES = 2

COMMIT_NUDGE_PROMPT = """\
You still have unpublished changes in the workspace. Before finishing, publish them yourself:
call `commit_changes` with a clear message for anything uncommitted, then `create_merge_request`
with a title and description to open (or update) the merge/pull request.

If you deliberately intend to leave the work unpublished, say so explicitly and stop."""


GIT_SYSTEM_PROMPT = SystemMessagePromptTemplate.from_template(
    """\
## Git context

- Git platform: {{git_platform}}
- Repository ID: {{repository}}
- Current branch: {{current_branch}}
- Default branch: {{default_branch}}
- Git status: nothing to commit, working tree clean (This is the git status at the start of the conversation. Note that this status is a snapshot in time, and will not update during the conversation.)

{{#agent_owns_commit}}
**You own committing.** When a change is ready, call `commit_changes` (with a clear message) and then `create_merge_request` (with a title and description) to publish it. Raw `git add`/`commit`/`push`/`reset`/`rebase`/`config` in bash remain hard-blocked by sandbox policy — use the tools, not bash, to publish. Read-only git (`git log`, `git diff`, `git status`, `git show`, `git branch`, `git ls-files`, …) stays allowed and useful for understanding branch state. If you leave changes uncommitted, the harness commits them for you as a safety net, but prefer authoring the merge/pull request yourself for a better title and description.

- If a task asks you to "rebase" or "resolve merge conflicts with the target branch," tell the user this harness does not support rebase-style workflows and stop. Do not try to emulate rebase with `git show`/`git checkout -- <paths>`; it cannot complete without a staging primitive.
{{/agent_owns_commit}}
{{^agent_owns_commit}}
**Committing and pushing is automatic.** The harness commits and pushes any file changes you make when your turn ends. You do not need to — and must not try to — run `git add`, `git commit`, `git push`, `git reset`, `git rebase`, `git config`, or any other index- or history-mutating git command. These are hard-blocked by sandbox policy; attempting them or their synonyms (`git stage`, `git update-index`, `git read-tree -m`, `git commit-tree`, …) will fail and waste turns.

- If a task tells you to "commit and push," interpret it as "make the edits" — the harness ships them.
- If a task asks you to "rebase" or "resolve merge conflicts with the target branch," tell the user this harness does not support rebase-style workflows and stop. Do not try to emulate rebase with `git show`/`git checkout -- <paths>`; it cannot complete without a staging primitive.
- Read-only git commands (`git log`, `git diff`, `git status`, `git show`, `git branch`, `git ls-files`, …) are allowed and useful for understanding branch state.
{{/agent_owns_commit}}
{{#issue_iid}}

You're currently working on issue #{{issue_iid}}.

The user will interact with you through the issue comments that will be automatically provided to you as messages. You should respond to the user's comments with the appropriate actions and tools.
{{/issue_iid}}

{{#merge_request_iid}}
You're currently working on merge request #{{merge_request_iid}}.

The user will interact with you through the merge request comments that will be automatically provided to you as messages. You should respond to the user's comments with the appropriate actions and tools.
{{/merge_request_iid}}""",  # noqa: E501
    "mustache",
)


class GitState(AgentState):
    """
    State for the git middleware.
    """

    merge_request: MergeRequest | None
    """
    The merge request used to commit the changes. Public on the output schema so
    it streams in AG-UI ``STATE_SNAPSHOT`` events — the chat UI's MR pill is wired
    directly to this field instead of a custom post-run event.
    """

    code_changes: Annotated[bool, PrivateStateAttr]
    """
    Whether the agent produced code changes that were published to the repository.
    """

    protected_branch_fallback_source: Annotated[str | None, PrivateStateAttr]
    """
    Source branch of the original MR when the publisher fell back to a fresh MR
    because that branch is protected on the remote. Consumed by managers so the
    notice can be appended to the agent's reply on the original MR rather than
    posted as a separate comment.
    """

    _git_nudges: Annotated[int, PrivateStateAttr]
    """
    How many times the safeguard has re-prompted the agent to publish its own work.
    Caps the nudge loop (see ``MAX_GIT_NUDGES``) before daiv publishes directly.
    """


class GitMiddleware(AgentMiddleware[GitState, RuntimeCtx]):
    """
    Middleware to handle the git operations and persist changes made by the DAIV agent to the repository.

    The middleware will commit and push the changes to the repository and create a merge request if necessary.
    The branch name and merge request ID will be stored in the state to be used later, ensuring that the same branch
    and merge request are used for subsequent commits.

    Args:
        skip_ci: Whether to skip the CI.

    Example:
        ```python
        from langchain.agents import create_agent
        from langgraph.store.memory import InMemoryStore
        from automation.agent.middlewares.git import GitMiddleware

        store = InMemoryStore()

        agent = create_agent(
            model="openai:gpt-4o",
            middleware=[GitMiddleware()],
            store=store,
        )
        ```
    """

    state_schema = GitState

    def __init__(
        self, *, skip_ci: bool = False, auto_commit_changes: bool = True, sandbox_enabled: bool = False
    ) -> None:
        """
        Initialize the middleware.
        """
        self.skip_ci = skip_ci
        self.auto_commit_changes = auto_commit_changes
        # The agent owns committing/MR creation only when it can act on a sandbox-authoritative
        # workspace and auto-commit is on. Otherwise these tools are absent, the agent is never
        # nudged, and the safeguard (``aafter_agent``) is the sole publisher.
        self._agent_owns_commit = sandbox_enabled and auto_commit_changes
        self.tools = [commit_changes, create_merge_request] if self._agent_owns_commit else []

    async def abefore_agent(self, state: GitState, runtime: Runtime[RuntimeCtx]) -> dict[str, Any] | None:
        """
        Before the agent starts, resolve the merge request the run will publish to.

        No branch checkout happens here: ``set_runtime_ctx`` clones directly on the right
        ref (``ref=source_branch`` for MR scope; chat persists the branch), and the sandbox
        is seeded from that clone — so the workspace already reflects the MR's source branch
        before this hook runs. A post-seed checkout would only touch the now non-authoritative
        local clone, so the resolved MR is just recorded in state for the prompt and safeguard.
        """
        merge_request = state.get("merge_request")

        if runtime.context.scope == Scope.MERGE_REQUEST:
            # In this case, ignore the branch name and merge request ID from the state,
            # and use the source branch and merge request ID from the merge request.
            merge_request = runtime.context.merge_request
        elif merge_request is None:
            # Surface any pre-existing open MR on the current branch so the chat
            # composer pill reflects reality from the very first turn. Issue-scope
            # runs always start on the default branch, where this lookup short-circuits.
            merge_request = await self._alookup_open_mr(runtime.context)

        return {"merge_request": merge_request, "code_changes": False, "protected_branch_fallback_source": None}

    @staticmethod
    async def _alookup_open_mr(context: RuntimeCtx) -> MergeRequest | None:
        """Best-effort lookup of an open MR whose source branch matches the current ref.

        Soft-fails on platform/transport errors so the agent can still run — the
        publisher will create a fresh MR if needed. Programming bugs propagate.
        """
        current_branch = get_repo_ref(context.gitrepo)
        if not current_branch or current_branch == context.config.default_branch:
            return None
        try:
            client = RepoClient.create_instance()
            return await sync_to_async(client.get_merge_request_by_branches)(
                context.repository.slug, current_branch, context.config.default_branch
            )
        except _MR_LOOKUP_PLATFORM_ERRORS:
            logger.exception(
                "Failed to look up open merge request for %s on %s", context.repository.slug, current_branch
            )
            return None

    async def awrap_model_call(
        self, request: ModelRequest[RuntimeCtx], handler: Callable[[ModelRequest[RuntimeCtx]], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """
        Update the system prompt with the git system prompt.
        """
        # Prefer the MR resolved by ``abefore_agent`` (in state) — for chat triggers
        # ``context.merge_request`` is unset even when the current branch has an open
        # MR. Without this fallback the agent re-discovers via
        # ``project-merge-request list --source-branch ...`` on every turn and may
        # pick a different MR than the publisher will write to.
        #
        # The state branch validates ``source_branch`` against the current ref —
        # ``abefore_agent`` populated state once at run start, but the branch
        # could have been checked out from under us mid-run (or the MR closed).
        # A stale id would then advertise the wrong MR on every subsequent turn.
        mr_iid: int | None = None
        if (ctx_mr := request.runtime.context.merge_request) is not None:
            mr_iid = ctx_mr.merge_request_id
        elif (state_mr := request.state.get("merge_request")) is not None:
            current_ref = get_repo_ref(request.runtime.context.gitrepo)
            if state_mr.source_branch == current_ref:
                mr_iid = state_mr.merge_request_id
            else:
                logger.warning(
                    "[%s] Ignoring stale state MR #%s: source_branch=%r != current ref=%r",
                    self.name,
                    state_mr.merge_request_id,
                    state_mr.source_branch,
                    current_ref,
                )

        context = {
            "git_platform": request.runtime.context.git_platform.value,
            "repository": request.runtime.context.repository.slug,
            "current_branch": get_repo_ref(request.runtime.context.gitrepo),
            "default_branch": request.runtime.context.config.default_branch,
            "issue_iid": request.runtime.context.issue.iid if request.runtime.context.issue else None,
            "merge_request_iid": mr_iid,
            "agent_owns_commit": self._agent_owns_commit,
        }

        system_prompt = ""
        if request.system_prompt:
            system_prompt = request.system_prompt + "\n\n"

        system_prompt += cast("str", GIT_SYSTEM_PROMPT.format(**context).content)

        return await handler(request.override(system_prompt=system_prompt))

    @staticmethod
    async def _is_unpublished(git_manager, state: GitState, context: RuntimeCtx) -> bool:
        """Whether the run produced changes that have not reached a merge request yet.

        Unpublished = uncommitted changes exist, OR there are changes versus the base branch
        that are not captured by a pushed MR (no MR recorded, or local commits not yet pushed
        to the MR's source branch). A clean tree with no diff versus base means there is
        nothing to publish.
        """
        if await git_manager.is_dirty():
            return True
        if not (await git_manager.get_diff(f"origin/{context.config.default_branch}")).strip():
            return False
        merge_request = state.get("merge_request")
        if merge_request is None:
            return True
        return await git_manager.has_unpushed(merge_request.source_branch)

    @staticmethod
    def _record_issue_mr(merge_request: MergeRequest, runtime: Runtime[RuntimeCtx]) -> None:
        """Tag the LangSmith run with the MR id when an issue produced a merge request."""
        if runtime.context.scope == Scope.ISSUE and (rt := get_current_run_tree()):
            rt.metadata["merge_request_id"] = merge_request.merge_request_id

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: GitState, runtime: Runtime[RuntimeCtx]) -> dict[str, Any] | None:
        """Nudge the agent to publish its own work before it stops.

        Runs after every model call but acts only at a terminal turn (a final assistant
        message with no tool calls). When the agent owns committing and left work unpublished,
        re-enter the model loop with an instruction to use its commit/MR tools, bounded by
        ``MAX_GIT_NUDGES``. Once nudges are exhausted, ``aafter_agent`` publishes directly.
        """
        if not self._agent_owns_commit:
            return None

        messages = state["messages"]
        last_message = messages[-1] if messages else None
        # Only act when the model is about to stop: a terminal assistant message with no
        # pending tool calls. A response with tool calls means the agent is still working.
        if not isinstance(last_message, AIMessage) or last_message.tool_calls:
            return None

        nudges_used = state.get("_git_nudges") or 0
        if nudges_used >= MAX_GIT_NUDGES:
            return None

        async with open_git_manager(session_id=state.get("session_id"), gitrepo=runtime.context.gitrepo) as git_manager:
            if not await self._is_unpublished(git_manager, state, runtime.context):
                return None

        logger.info(
            "[%s] Unpublished changes at turn end; nudging agent to publish (attempt %s/%s).",
            self.name,
            nudges_used + 1,
            MAX_GIT_NUDGES,
        )
        return {
            "messages": [HumanMessage(content=COMMIT_NUDGE_PROMPT)],
            "_git_nudges": nudges_used + 1,
            "jump_to": "model",
        }

    async def aafter_agent(self, state: GitState, runtime: Runtime[RuntimeCtx]) -> dict[str, Any] | None:
        """
        Safeguard: guarantee the run's changes are published, completing anything the agent left.

        Happy path: the agent committed and opened/updated the MR via its own tools, so the
        MR already lives in state — just confirm it. Otherwise (agent ignored the nudges, or
        the tools are unavailable for this run) daiv publishes directly via the publisher,
        which generates the title/description/commit message with ``_diff_to_metadata``.
        """
        if not self.auto_commit_changes:
            return None

        async with open_git_manager(session_id=state.get("session_id"), gitrepo=runtime.context.gitrepo) as git_manager:
            unpublished = await self._is_unpublished(git_manager, state, runtime.context)

        if not unpublished:
            # Published by the agent, or no changes at all.
            merge_request = state.get("merge_request")
            if merge_request:
                self._record_issue_mr(merge_request, runtime)
                return {"merge_request": merge_request, "code_changes": True}
            return None

        # Daiv-direct fallback: commit/push the work and open/update the MR.
        publisher = GitChangePublisher(runtime.context)
        merge_request = await publisher.publish(
            session_id=state.get("session_id"), merge_request=state.get("merge_request"), skip_ci=self.skip_ci
        )
        if merge_request:
            self._record_issue_mr(merge_request, runtime)
            return {
                "merge_request": merge_request,
                "code_changes": True,
                "protected_branch_fallback_source": publisher.protected_branch_fallback_source,
            }

        return None
