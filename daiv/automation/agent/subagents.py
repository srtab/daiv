import logging
import re
from typing import TYPE_CHECKING

import yaml
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

logger = logging.getLogger("daiv.agent")

GENERAL_PURPOSE_DESCRIPTION = "General-purpose agent for researching complex questions, searching for code, and executing multi-step tasks. When you are searching for a keyword or file and are not confident that you will find the right match in the first few tries use this agent to perform the search for you. This agent has access to all tools as the main agent."  # noqa: E501

GENERAL_PURPOSE_SYSTEM_PROMPT = """You are an agent for DAIV. Given the user's message, you should use the tools available to complete the task. Do exactly what has been asked. When you complete the task respond with a detailed writeup.

- For file searches: Use `grep` or `glob` when you need to search broadly. Use `read_file` when you know the specific file path.
- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested.
- CRITICAL: All file paths in your response MUST be absolute paths exactly as returned by the tools (e.g., /repo/src/app/utils.py). Never strip prefixes or convert to relative paths — the caller uses your paths directly in tool calls.
"""  # noqa: E501


def _build_general_purpose_middleware(
    model: BaseChatModel,
    backend: BackendProtocol,
    runtime: RuntimeCtx,
    sandbox_enabled: bool,
    web_search_enabled: bool,
    web_fetch_enabled: bool,
) -> list:
    """
    Build the middleware stack for a general-purpose subagent.
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

    return middleware


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
    return SubAgent(
        name="general-purpose",
        description=GENERAL_PURPOSE_DESCRIPTION,
        system_prompt=GENERAL_PURPOSE_SYSTEM_PROMPT,
        middleware=_build_general_purpose_middleware(
            model, backend, runtime, sandbox_enabled, web_search_enabled, web_fetch_enabled
        ),
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


FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_subagent_frontmatter(content: str, file_path: str) -> tuple[dict, str] | None:
    """
    Parse YAML frontmatter and body from a subagent markdown file.

    Args:
        content: The full file content.
        file_path: Path to the file (for logging).

    Returns:
        Tuple of (frontmatter dict, body string), or None if parsing fails.
    """
    match = FRONTMATTER_PATTERN.match(content)
    if not match:
        logger.warning("Skipping %s: no valid YAML frontmatter found", file_path)
        return None

    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as e:
        logger.warning("Invalid YAML in %s: %s", file_path, e)
        return None

    if not isinstance(frontmatter, dict):
        logger.warning("Skipping %s: frontmatter is not a mapping", file_path)
        return None

    name = str(frontmatter.get("name", "")).strip()
    description = str(frontmatter.get("description", "")).strip()
    if not name or not description:
        logger.warning("Skipping %s: missing required 'name' or 'description'", file_path)
        return None

    frontmatter["name"] = name
    frontmatter["description"] = description

    body = content[match.end() :].strip()
    if not body:
        logger.warning("Skipping %s: empty body (system prompt)", file_path)
        return None

    return frontmatter, body


async def load_custom_subagents(
    model: BaseChatModel,
    backend: BackendProtocol,
    runtime: RuntimeCtx,
    sources: list[str],
    sandbox_enabled: bool = True,
    web_search_enabled: bool = True,
    web_fetch_enabled: bool = True,
) -> list[SubAgent]:
    """
    Load custom subagents from markdown files in the given source paths.

    Each source path is scanned for .md files. Each file should contain YAML frontmatter
    with ``name`` and ``description`` fields, and a markdown body that becomes the system prompt.

    Args:
        model: The default model to use for custom subagents.
        backend: The filesystem backend.
        runtime: The runtime context.
        sources: List of paths to scan for subagent definitions.
        sandbox_enabled: Whether to enable the sandbox middleware.
        web_search_enabled: Whether to enable web search middleware.
        web_fetch_enabled: Whether to enable web fetch middleware.

    Returns:
        List of SubAgent dicts for the loaded custom subagents.
    """
    subagents: list[SubAgent] = []

    for source_path in sources:
        try:
            items = await backend.als_info(source_path)
        except Exception:
            logger.debug("Could not list %s, skipping custom subagents from this source", source_path)
            continue

        md_files = [item["path"] for item in items if not item.get("is_dir") and item["path"].endswith(".md")]
        if not md_files:
            continue

        responses = await backend.adownload_files(md_files)

        for file_path, response in zip(md_files, responses, strict=True):
            if response.error:
                continue
            if response.content is None:
                continue

            try:
                content = response.content.decode("utf-8")
            except UnicodeDecodeError as e:
                logger.warning("Error decoding %s: %s", file_path, e)
                continue

            parsed = _parse_subagent_frontmatter(content, file_path)
            if parsed is None:
                continue

            frontmatter, body = parsed

            subagent_model = model
            if frontmatter_model := str(frontmatter.get("model", "")).strip():
                try:
                    subagent_model = BaseAgent.get_model(model=frontmatter_model)
                except Exception:
                    logger.warning("Skipping %s: invalid model '%s'", file_path, frontmatter_model)
                    continue

            subagents.append(
                SubAgent(
                    name=frontmatter["name"],
                    description=frontmatter["description"],
                    system_prompt=body,
                    middleware=_build_general_purpose_middleware(
                        subagent_model, backend, runtime, sandbox_enabled, web_search_enabled, web_fetch_enabled
                    ),
                    model=subagent_model,
                    tools=[],
                )
            )

            logger.info("Loaded custom subagent '%s' from %s", frontmatter["name"], file_path)

    return subagents
