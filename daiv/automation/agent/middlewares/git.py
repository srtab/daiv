from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any, cast

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.prompts import SystemMessagePromptTemplate
from langsmith import get_current_run_tree

from automation.agent.publishers import GitChangePublisher
from codebase.base import MergeRequest, Scope
from codebase.context import RuntimeCtx  # noqa: TC001
from codebase.utils import GitManager, GitPushPermissionError, get_repo_ref

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langgraph.runtime import Runtime


logger = logging.getLogger("daiv.tools")


GIT_SYSTEM_PROMPT = SystemMessagePromptTemplate.from_template(
    """\
## Git context

- Git platform: {{git_platform}}
- Repository ID: {{repository}}
- Current branch: {{current_branch}}
- Default branch: {{default_branch}}
- Git status: nothing to commit, working tree clean (This is the git status at the start of the conversation. Note that this status is a snapshot in time, and will not update during the conversation.)
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

    merge_request: Annotated[MergeRequest | None, PrivateStateAttr]
    """
    The merge request used to commit the changes.
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

    def __init__(self, *, skip_ci: bool = False, auto_commit_changes: bool = True) -> None:
        """
        Initialize the middleware.
        """
        self.skip_ci = skip_ci
        self.auto_commit_changes = auto_commit_changes

    async def abefore_agent(self, state: GitState, runtime: Runtime[RuntimeCtx]) -> dict[str, Any] | None:
        """
        Before the agent starts, set the branch name and merge request ID.
        """
        merge_request = state.get("merge_request")

        if runtime.context.scope == Scope.MERGE_REQUEST:
            # In this case, ignore the branch name and merge request ID from the state,
            # and use the source branch and merge request ID from the merge request.
            merge_request = runtime.context.merge_request

        if merge_request and merge_request.source_branch != get_repo_ref(runtime.context.repo):
            git_manager = GitManager(runtime.context.repo)

            logger.info("[%s] Checking out to branch '%s'", self.name, merge_request.source_branch)

            try:
                git_manager.checkout(merge_request.source_branch)
            except ValueError as e:
                # The branch does not exist in the repository, so we need to create it.
                logger.warning("[%s] Failed to checkout to branch '%s': %s", self.name, merge_request.source_branch, e)
                merge_request = None

        return {"merge_request": merge_request}

    async def awrap_model_call(
        self, request: ModelRequest[RuntimeCtx], handler: Callable[[ModelRequest[RuntimeCtx]], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """
        Update the system prompt with the git system prompt.
        """
        context = {
            "git_platform": request.runtime.context.git_platform.value,
            "repository": request.runtime.context.repo_id,
            "current_branch": get_repo_ref(request.runtime.context.repo),
            "default_branch": request.runtime.context.config.default_branch,
            "issue_iid": request.runtime.context.issue.iid if request.runtime.context.issue else None,
            "merge_request_iid": request.runtime.context.merge_request.merge_request_id
            if request.runtime.context.merge_request
            else None,
        }

        system_prompt = ""
        if request.system_prompt:
            system_prompt = request.system_prompt + "\n\n"

        system_prompt += cast("str", GIT_SYSTEM_PROMPT.format(**context).content)

        return await handler(request.override(system_prompt=system_prompt))

    async def aafter_agent(self, state: GitState, runtime: Runtime[RuntimeCtx]) -> dict[str, Any] | None:
        """
        After the agent finishes, commit the changes and update or create the merge request.
        """
        if not self.auto_commit_changes:
            return None

        publisher = GitChangePublisher(runtime.context)
        try:
            merge_request = await publisher.publish(merge_request=state.get("merge_request"), skip_ci=self.skip_ci)
        except GitPushPermissionError as e:
            logger.warning("[%s] Failed to publish changes due to git push permissions: %s", self.name, e)
            return None

        if merge_request:
            if runtime.context.scope == Scope.ISSUE and (rt := get_current_run_tree()):
                # If an issue resulted in a merge request, we send it to LangSmith for tracking.
                rt.metadata["merge_request_id"] = merge_request.merge_request_id

            return {"merge_request": merge_request}

        return None
