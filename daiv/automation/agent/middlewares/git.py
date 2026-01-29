from __future__ import annotations

import logging
from textwrap import dedent
from typing import TYPE_CHECKING, Any

from django.template.loader import render_to_string

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.prompts import SystemMessagePromptTemplate

from automation.agent.pr_describer.graph import create_pr_describer_agent
from codebase.base import GitPlatform, MergeRequest, Scope
from codebase.clients import RepoClient
from codebase.context import RuntimeCtx  # noqa: TC001
from codebase.utils import GitManager, redact_diff_content
from core.constants import BOT_LABEL, BOT_NAME

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langgraph.runtime import Runtime

    from automation.agent.pr_describer.schemas import PullRequestMetadata


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

    branch_name: str
    """
    The branch name used to commit the changes.
    """

    merge_request_id: int
    """
    The merge request ID used to commit the changes.
    """


class GitMiddleware(AgentMiddleware):
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
        branch_name = state.get("branch_name")
        merge_request_id = state.get("merge_request_id")

        if runtime.context.scope == Scope.MERGE_REQUEST:
            # In this case, ignore the branch name and merge request ID from the state,
            # and use the source branch and merge request ID from the merge request.
            branch_name = runtime.context.merge_request.source_branch
            merge_request_id = runtime.context.merge_request.merge_request_id

        if branch_name and branch_name != runtime.context.repo.active_branch.name:
            git_manager = GitManager(runtime.context.repo)

            logger.info("[%s] Checking out to branch '%s'", self.name, branch_name)

            try:
                git_manager.checkout(branch_name)
            except ValueError as e:
                # The branch does not exist in the repository, so we need to create it.
                logger.warning("[%s] Failed to checkout to branch '%s': %s", self.name, branch_name, e)
                branch_name = None
                merge_request_id = None

        return {"branch_name": branch_name, "merge_request_id": merge_request_id}

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """
        Update the system prompt with the git system prompt.
        """
        context = {
            "git_platform": request.runtime.context.git_platform.value,
            "repository": request.runtime.context.repo_id,
            "current_branch": request.runtime.context.repo.active_branch.name,
            "default_branch": request.runtime.context.config.default_branch,
            "issue_iid": request.runtime.context.issue.iid if request.runtime.context.issue else None,
            "merge_request_iid": request.runtime.context.merge_request.merge_request_id
            if request.runtime.context.merge_request
            else None,
        }

        system_prompt = GIT_SYSTEM_PROMPT.format(**context).content

        request = request.override(system_prompt=request.system_prompt + "\n\n" + system_prompt)

        return await handler(request)

    async def aafter_agent(self, state: GitState, runtime: Runtime[RuntimeCtx]) -> dict[str, Any] | None:
        """
        After the agent finishes, commit the changes and update or create the merge request.
        """
        if not self.auto_commit_changes:
            return None

        git_manager = GitManager(runtime.context.repo)

        if not git_manager.is_dirty():
            return None

        pr_metadata = await self._get_mr_metadata(runtime, git_manager.get_diff())
        branch_name = state.get("branch_name") or pr_metadata.branch

        logger.info("[%s] Committing and pushing changes to branch '%s'", self.name, branch_name)

        unique_branch_name = git_manager.commit_and_push_changes(
            pr_metadata.commit_message,
            branch_name=branch_name,
            skip_ci=self.skip_ci,
            use_branch_if_exists=bool(state.get("branch_name")),
        )

        merge_request_id = state.get("merge_request_id")
        if runtime.context.scope != Scope.MERGE_REQUEST and not merge_request_id:
            logger.info(
                "[%s] Creating merge request: '%s' -> '%s'",
                self.name,
                unique_branch_name,
                runtime.context.config.default_branch,
            )
            merge_request = self._update_or_create_merge_request(
                runtime, unique_branch_name, pr_metadata.title, pr_metadata.description
            )
            merge_request_id = merge_request.merge_request_id
            logger.info("[%s] Merge request created: %s", self.name, merge_request.web_url)

        return {"branch_name": unique_branch_name, "merge_request_id": merge_request_id}

    async def _get_mr_metadata(self, runtime: Runtime[RuntimeCtx], diff: str) -> PullRequestMetadata:
        """
        Get the PR metadata from the diff.

        Args:
            runtime: The runtime context.
            diff: The diff of the changes.

        Returns:
            The PR metadata.
        """
        pr_describer = create_pr_describer_agent(
            model=runtime.context.config.models.pr_describer.model, ctx=runtime.context
        )

        extra_context = ""
        if runtime.context.scope == Scope.ISSUE:
            extra_context = dedent(
                """\
                This changes were made to address the following issue:

                Issue ID: {issue.iid}
                Issue title: {issue.title}
                Issue description: {issue.description}
                """
            ).format(issue=runtime.context.issue)

        result = await pr_describer.ainvoke(
            {
                "diff": redact_diff_content(diff, runtime.context.config.omit_content_patterns),
                "extra_context": extra_context,
            },
            config={
                "tags": [pr_describer.get_name(), runtime.context.git_platform.value],
                "metadata": {"scope": runtime.context.scope, "repo_id": runtime.context.repo_id},
            },
        )
        if result and "structured_response" in result:
            return result["structured_response"]

        raise ValueError("Failed to get PR metadata from the diff.")

    def _update_or_create_merge_request(
        self, runtime: Runtime[RuntimeCtx], branch_name: str, title: str, description: str
    ) -> MergeRequest:
        """
        Update or create the merge request.

        Args:
            runtime: The runtime context.
            branch_name: The branch name.
            title: The title of the merge request.
            description: The description of the merge request.
        """
        assignee_id = None

        if runtime.context.issue and runtime.context.issue.assignee:
            assignee_id = (
                runtime.context.issue.assignee.id
                if runtime.context.git_platform == GitPlatform.GITLAB
                else runtime.context.issue.assignee.username
            )

        client = RepoClient.create_instance()
        return client.update_or_create_merge_request(
            repo_id=runtime.context.repo_id,
            source_branch=branch_name,
            target_branch=runtime.context.config.default_branch,
            labels=[BOT_LABEL],
            title=title,
            assignee_id=assignee_id,
            description=render_to_string(
                "codebase/issue_merge_request.txt",
                {
                    "description": description,
                    "source_repo_id": runtime.context.repo_id,
                    "issue_id": runtime.context.issue.iid if runtime.context.issue else None,
                    "bot_name": BOT_NAME,
                    "bot_username": runtime.context.bot_username,
                    "is_gitlab": runtime.context.git_platform == GitPlatform.GITLAB,
                },
            ),
        )
