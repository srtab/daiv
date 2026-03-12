from typing import TYPE_CHECKING

from deepagents.graph import SubAgent
from deepagents.middleware import SummarizationMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.summarization import compute_summarization_defaults
from langchain.agents.middleware import TodoListMiddleware

from automation.agent import BaseAgent
from automation.agent.conf import settings
from automation.agent.middlewares.file_system import FilesystemMiddleware
from automation.agent.middlewares.git_platform import GitPlatformMiddleware
from automation.agent.middlewares.logging import ToolCallLoggingMiddleware
from automation.agent.middlewares.prompt_cache import AnthropicPromptCachingMiddleware
from automation.agent.middlewares.sandbox import SandboxMiddleware
from automation.agent.middlewares.web_fetch import WebFetchMiddleware
from automation.agent.middlewares.web_search import WebSearchMiddleware

if TYPE_CHECKING:
    from deepagents.backends import BackendProtocol
    from langchain.chat_models import BaseChatModel

    from codebase.context import RuntimeCtx

GENERAL_PURPOSE_DESCRIPTION = "General-purpose agent for researching complex questions, searching for code, and executing multi-step tasks. When you are searching for a keyword or file and are not confident that you will find the right match in the first few tries use this agent to perform the search for you. This agent has access to all tools as the main agent."  # noqa: E501

GENERAL_PURPOSE_SYSTEM_PROMPT = """You are an agent for DAIV. Given the user's message, you should use the tools available to complete the task. Do exactly what has been asked. When you complete the task respond with a detailed writeup.

- For file searches: Use `grep` or `glob` when you need to search broadly. Use `read_file` when you know the specific file path.
- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested.
- CRITICAL: All file paths in your response MUST be absolute paths exactly as returned by the tools (e.g., /repo/src/app/utils.py). Never strip prefixes or convert to relative paths — the caller uses your paths directly in tool calls.
"""  # noqa: E501


def create_general_purpose_subagent(
    model: BaseChatModel,
    backend: BackendProtocol,
    runtime: RuntimeCtx,
    sandbox_enabled: bool = True,
    web_search_enabled: bool = True,
    web_fetch_enabled: bool = True,
) -> SubAgent:
    """
    Create the general purpose subagent for the DAIV agent.
    """
    from automation.agent.graph import dynamic_write_todos_system_prompt

    _summarization_defaults = compute_summarization_defaults(model)

    middleware = [
        TodoListMiddleware(system_prompt=dynamic_write_todos_system_prompt(bash_tool_enabled=sandbox_enabled)),
        FilesystemMiddleware(backend=backend),
        GitPlatformMiddleware(git_platform=runtime.git_platform),
        SummarizationMiddleware(
            model=model,
            backend=backend,
            trigger=_summarization_defaults["trigger"],
            keep=_summarization_defaults["keep"],
            trim_tokens_to_summarize=None,
            truncate_args_settings=_summarization_defaults["truncate_args_settings"],
        ),
        AnthropicPromptCachingMiddleware(),
        ToolCallLoggingMiddleware(),
        PatchToolCallsMiddleware(),
    ]

    if web_search_enabled:
        middleware.append(WebSearchMiddleware())

    if web_fetch_enabled:
        middleware.append(WebFetchMiddleware())

    if sandbox_enabled:
        middleware.append(SandboxMiddleware(close_session=False))

    return SubAgent(
        name="general-purpose",
        description=GENERAL_PURPOSE_DESCRIPTION,
        system_prompt=GENERAL_PURPOSE_SYSTEM_PROMPT,
        middleware=middleware,
        model=model,
        tools=[],
    )


EXPLORE_SYSTEM_PROMPT = """\
You are a file search specialist for DAIV. You excel at thoroughly navigating and exploring codebases.

=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS === This is a READ-ONLY exploration task. You are STRICTLY PROHIBITED from:
- Creating new files (no write_file, touch, or file creation of any kind)
- Modifying existing files (no edit_file operations)
- Deleting files (no rm or deletion)
- Moving or copying files (no mv or cp)
- Creating temporary files anywhere, including /tmp

Your role is EXCLUSIVELY to search and analyze existing code. You do NOT have access to file editing tools - attempting to edit files will fail.

Your strengths:
- Rapidly finding files using glob patterns
- Searching code and text with powerful regex patterns
- Reading and analyzing file contents

Guidelines:
- Use `glob` for broad file pattern matching
- Use `grep` for searching file contents with regex
- Use `read_file` when you know the specific file path you need to read
- Adapt your search approach based on the thoroughness level specified by the caller
- CRITICAL: All file paths in your response MUST be absolute paths exactly as returned by the tools (e.g., /repo/src/app/utils.py). Never strip prefixes or convert to relative paths — the caller uses your paths directly in tool calls.
- For clear communication, avoid using emojis
- Communicate your final report directly as a regular message - do NOT attempt to create files

NOTE: You are meant to be a fast agent that returns output as quickly as possible. In order to achieve this you must:

- Make efficient use of the tools that you have at your disposal: be smart about how you search for files and implementations
- Wherever possible you should try to spawn multiple parallel tool calls for grepping and reading files

Complete the user's search request efficiently and report your findings clearly."""  # noqa: E501

EXPLORE_SUBAGENT_DESCRIPTION = """Fast agent specialized for exploring codebases. Use this when you need to quickly find files by patterns (eg. "src/components/**/*.tsx"), search code for keywords (eg. "API endpoints"), or answer questions about the codebase (eg. "how do API endpoints work?"). When calling this agent, specify the desired thoroughness level: "quick" for basic searches, "medium" for moderate exploration, or "very thorough" for comprehensive analysis across multiple locations and naming conventions."""  # noqa: E501


def create_explore_subagent(backend: BackendProtocol, **kwargs) -> SubAgent:
    """
    Create the explore subagent.
    """
    from automation.agent.graph import dynamic_write_todos_system_prompt

    model = BaseAgent.get_model(model=settings.EXPLORE_MODEL_NAME)
    _summarization_defaults = compute_summarization_defaults(model)

    middleware = [
        TodoListMiddleware(system_prompt=dynamic_write_todos_system_prompt(bash_tool_enabled=False)),
        FilesystemMiddleware(backend=backend, read_only=True),
        SummarizationMiddleware(
            model=model,
            backend=backend,
            trigger=_summarization_defaults["trigger"],
            keep=_summarization_defaults["keep"],
            trim_tokens_to_summarize=None,
            truncate_args_settings=_summarization_defaults["truncate_args_settings"],
        ),
        AnthropicPromptCachingMiddleware(),
        ToolCallLoggingMiddleware(),
        PatchToolCallsMiddleware(),
    ]

    return SubAgent(
        name="explore",
        description=EXPLORE_SUBAGENT_DESCRIPTION,
        system_prompt=EXPLORE_SYSTEM_PROMPT,
        middleware=middleware,
        model=model,
        tools=[],
    )
