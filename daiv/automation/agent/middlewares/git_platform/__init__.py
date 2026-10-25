from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, NotRequired

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import OmitFromOutput
from langchain.tools import ToolRuntime, tool
from langgraph.types import Command  # noqa: TC002

from automation.agent.context import RuntimeCtx  # noqa: TC001
from automation.agent.middlewares.git_platform.github import GITHUB_TOOL_NAME, run_github_subcommand
from automation.agent.middlewares.git_platform.gitlab import GITLAB_TOOL_NAME, run_gitlab_subcommand
from automation.agent.middlewares.git_platform.prompts import (
    CROSS_PROJECT_TOOL_DESCRIPTION,
    GIT_PLATFORM_SYSTEM_PROMPT,
    GITHUB_TOOL_DESCRIPTION,
    GITLAB_TOOL_DESCRIPTION,
    PROJECT_ARG_DESCRIPTION,
)
from codebase.base import GitPlatform

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from deepagents.backends.protocol import BackendProtocol
    from langchain_core.tools import BaseTool


# Shared by the with- and without-``project`` shape of each tool, so the two cannot drift in what they tell the model.
GitLabSubcommandArg = Annotated[
    str,
    "Single GitLab CLI subcommand string, parsed with shell-like quoting. "
    "Format: '<object> <action> [arguments...]'. "
    "Do not include the 'gitlab' prefix, '--project-id', or any output/verbosity flags "
    "managed by the tool. Wrap arguments containing spaces in double quotes. "
    "Examples: 'project-issue get --iid 42', 'project-merge-request list --state opened'.",
]

GitHubSubcommandArg = Annotated[
    str,
    "Single GitHub CLI subcommand string, parsed with shell-like quoting. "
    "Format: '<object> <action> [arguments...]'. "
    "Do not include the 'gh' prefix. "
    "Do not pass '--repo', '-R', or '--hostname' (these are managed or restricted by the tool). "
    "Wrap arguments containing spaces in double quotes. "
    "Examples: 'issue view 42', 'pr list --state open'.",
]

OutputModeArg = Annotated[
    Literal["detailed", "simplified"],
    "Controls the detail level of the returned output. "
    "Use 'simplified' for listing/discovery and 'detailed' for inspecting a specific "
    "resource or reading logs. Ignored when output_to_file is true (JSON is forced). "
    "Default: 'simplified'.",
]

GitLabOutputToFileArg = Annotated[
    bool,
    "Set true to write the FULL result to a file for bash/jq/grep instead of returning it "
    "inline. The path is auto-assigned under the large-tool-results dir and returned in a "
    "compact confirmation (path + size + short preview). The result is written as JSON "
    "(output_mode ignored); a project-job trace is written as raw log text. Use this when "
    "you intend to post-process the result with bash. Default: false.",
]

GitHubOutputToFileArg = Annotated[
    bool,
    "Set true to write the FULL stdout to a file for bash/jq/grep instead of returning it "
    "inline. The path is auto-assigned under the large-tool-results dir and returned in a "
    "compact confirmation (path + size + short preview). gh has no global JSON flag — for "
    "jq-able output include gh's own --json <fields> [--jq ...] in the subcommand. Use this "
    "when you intend to post-process the result with bash. Default: false.",
]

ProjectArg = Annotated[str, PROJECT_ARG_DESCRIPTION]


class GitPlatformState(AgentState):
    github_token: NotRequired[Annotated[str | None, OmitFromOutput]]
    github_token_expires_at: NotRequired[Annotated[float | None, OmitFromOutput]]


class GitPlatformMiddleware(AgentMiddleware):
    """
    Middleware to add the git platform tools to the agent.

    Example:
        ```python
        from langchain.agents import create_agent
        from langgraph.store.memory import InMemoryStore
        from codebase.base import GitPlatform
        from automation.agent.middlewares.git_platform import GitPlatformMiddleware

        store = InMemoryStore()

        agent = create_agent(
            model="openai:gpt-4o",
            # ``backend`` is the agent's filesystem backend (e.g. the shared SandboxFileBackend).
            middleware=[GitPlatformMiddleware(git_platform=GitPlatform.GITHUB, backend=backend)],
            store=store,
        )
        ```
    """

    state_schema = GitPlatformState

    def __init__(
        self, git_platform: GitPlatform, backend: BackendProtocol, cross_project_enabled: bool = False
    ) -> None:
        """
        Initialize the middleware.

        ``backend`` is the same filesystem backend the agent's file tools use; the platform tools
        write ``to_file`` dumps through it, into the same large-tool-results dir the deepagents
        FilesystemMiddleware auto-evicts to (derived identically from the backend's artifacts root).

        ``cross_project_enabled`` off — the default — builds exactly the tool that existed before
        this feature: no ``project`` argument in the schema, and the cross-project branch
        unreachable.
        """
        super().__init__()

        self._backend = backend
        self._cross_project_enabled = cross_project_enabled

        self.tools = []

        if git_platform == GitPlatform.GITLAB:
            self.tools.append(self._build_gitlab_tool())
        elif git_platform == GitPlatform.GITHUB:
            self.tools.append(self._build_github_tool())

    def _build_gitlab_tool(self) -> BaseTool:
        """Build the ``gitlab`` tool as a closure over the bound backend.

        Two shapes, because a capability that is off must not appear in the schema at all: the
        argument list is what the model sees, and an argument documented as unavailable is worse
        than an absent one.
        """
        backend = self._backend

        if not self._cross_project_enabled:

            @tool(GITLAB_TOOL_NAME, description=GITLAB_TOOL_DESCRIPTION)
            async def gitlab(
                subcommand: GitLabSubcommandArg,
                runtime: ToolRuntime[RuntimeCtx],
                output_mode: OutputModeArg = "simplified",
                output_to_file: GitLabOutputToFileArg = False,
            ) -> str:
                return await run_gitlab_subcommand(subcommand, runtime, output_mode, output_to_file, backend=backend)

            return gitlab

        @tool(GITLAB_TOOL_NAME, description=GITLAB_TOOL_DESCRIPTION + CROSS_PROJECT_TOOL_DESCRIPTION)
        async def gitlab_cross_project(
            subcommand: GitLabSubcommandArg,
            runtime: ToolRuntime[RuntimeCtx],
            project: ProjectArg = "",
            output_mode: OutputModeArg = "simplified",
            output_to_file: GitLabOutputToFileArg = False,
        ) -> str:
            return await run_gitlab_subcommand(
                subcommand, runtime, output_mode, output_to_file, backend=backend, project=project
            )

        return gitlab_cross_project

    def _build_github_tool(self) -> BaseTool:
        """Build the ``gh`` tool as a closure over the bound backend, in the same two shapes as ``gitlab``."""
        backend = self._backend

        if not self._cross_project_enabled:

            @tool(GITHUB_TOOL_NAME, description=GITHUB_TOOL_DESCRIPTION)
            async def github(
                subcommand: GitHubSubcommandArg,
                runtime: ToolRuntime[RuntimeCtx],
                output_to_file: GitHubOutputToFileArg = False,
            ) -> str | Command:
                return await run_github_subcommand(subcommand, runtime, output_to_file, backend=backend)

            return github

        @tool(GITHUB_TOOL_NAME, description=GITHUB_TOOL_DESCRIPTION + CROSS_PROJECT_TOOL_DESCRIPTION)
        async def github_cross_project(
            subcommand: GitHubSubcommandArg,
            runtime: ToolRuntime[RuntimeCtx],
            project: ProjectArg = "",
            output_to_file: GitHubOutputToFileArg = False,
        ) -> str | Command:
            return await run_github_subcommand(
                subcommand, runtime, output_to_file, backend=backend, project=project, cross_project_enabled=True
            )

        return github_cross_project

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """
        Update the system prompt with the git platform system prompt.
        """
        if request.runtime.context.git_platform in {GitPlatform.GITLAB, GitPlatform.GITHUB}:
            git_platform_system_prompt = GIT_PLATFORM_SYSTEM_PROMPT.format(
                gitlab_platform=request.runtime.context.git_platform == GitPlatform.GITLAB,
                github_platform=request.runtime.context.git_platform == GitPlatform.GITHUB,
                cross_project=self._cross_project_enabled,
            )
            request = request.override(
                system_prompt=request.system_prompt + "\n\n" + git_platform_system_prompt.content
            )

        return await handler(request)
