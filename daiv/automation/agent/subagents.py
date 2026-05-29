import logging
import re
from typing import TYPE_CHECKING, Any

import yaml
from deepagents.middleware import SummarizationMiddleware
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.subagents import CompiledSubAgent
from deepagents.middleware.summarization import compute_summarization_defaults
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelFallbackMiddleware, TodoListMiddleware

from automation.agent import BaseAgent
from automation.agent.constants import REPO_PATH, WORKSPACE_PATH
from automation.agent.middlewares.file_system import (
    CUSTOM_TOOL_DESCRIPTIONS,
    WORKSPACE_ARTIFACT_SUBTREES,
    WORKSPACE_FENCE_PERMISSIONS,
    WORKSPACE_FENCE_SUBTREES,
    filesystem_absolute_path_directive,
)
from automation.agent.middlewares.git_platform import GitPlatformMiddleware
from automation.agent.middlewares.logging import ToolCallLoggingMiddleware
from automation.agent.middlewares.prompt_cache import AnthropicPromptCachingMiddleware
from automation.agent.middlewares.sandbox import SandboxMiddleware
from automation.agent.middlewares.web_fetch import WebFetchMiddleware
from automation.agent.middlewares.web_search import WebSearchMiddleware
from core.site_settings import site_settings

if TYPE_CHECKING:
    from deepagents.backends import BackendProtocol
    from langchain.chat_models import BaseChatModel

    from automation.agent.middlewares.file_system import SandboxFileBackend
    from codebase.context import RuntimeCtx
    from core.sandbox.client import DAIVSandboxClient

GENERAL_PURPOSE_NAME = "general-purpose"
EXPLORE_NAME = "explore"

logger = logging.getLogger("daiv.agent")

GENERAL_PURPOSE_DESCRIPTION = "General-purpose agent for researching complex questions, searching for code, and executing multi-step tasks. When you are searching for a keyword or file and are not confident that you will find the right match in the first few tries use this agent to perform the search for you. This agent has access to all tools as the main agent."  # noqa: E501


def _general_purpose_system_prompt(working_directory: str) -> str:
    root = working_directory.rstrip("/") + "/"
    return f"""You are an agent for DAIV. Given the user's message, you should use the tools available to complete the task. Do exactly what has been asked. When you complete the task respond with a detailed writeup.

- Your working directory is {root}.
- For file searches: Use `grep` or `glob` when you need to search broadly. Use `read_file` when you know the specific file path.
- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested.
- CRITICAL: All file paths in your response MUST be absolute paths exactly as returned by the tools (e.g., {root}src/app/utils.py). Never strip prefixes or convert to relative paths — the caller uses your paths directly in tool calls.

{filesystem_absolute_path_directive(working_directory)}
"""  # noqa: E501


def _build_general_purpose_middleware(
    model: BaseChatModel,
    backend: BackendProtocol,
    runtime: RuntimeCtx,
    sandbox_enabled: bool,
    web_search_enabled: bool,
    web_fetch_enabled: bool,
    fallback_models: list[BaseChatModel] | None = None,
    client: DAIVSandboxClient | None = None,
    sandbox_backend: SandboxFileBackend | None = None,
) -> list:
    """
    Build the middleware stack for a general-purpose subagent.

    ``close_session=False`` lets the subagent reuse the parent agent's sandbox session.
    """
    # Local import to break a circular dependency: graph.py imports this module.
    from automation.agent.graph import dynamic_write_todos_system_prompt

    _summarization_defaults = compute_summarization_defaults(model)

    middleware: list[AgentMiddleware[Any, Any, Any]] = [
        TodoListMiddleware(system_prompt=dynamic_write_todos_system_prompt(bash_tool_enabled=sandbox_enabled)),
        FilesystemMiddleware(
            backend=backend,
            custom_tool_descriptions=CUSTOM_TOOL_DESCRIPTIONS,
            _permissions=None if sandbox_enabled else WORKSPACE_FENCE_PERMISSIONS,
        ),
        GitPlatformMiddleware(git_platform=runtime.git_platform, backend=backend),
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
        middleware.append(
            SandboxMiddleware(agent_root=REPO_PATH, client=client, sandbox_backend=sandbox_backend, close_session=False)
        )

    if fallback_models:
        middleware.append(ModelFallbackMiddleware(*fallback_models))

    return middleware


def create_general_purpose_subagent(
    model: BaseChatModel,
    backend: BackendProtocol,
    runtime: RuntimeCtx,
    working_directory: str,
    sandbox_enabled: bool = True,
    web_search_enabled: bool = True,
    web_fetch_enabled: bool = True,
    fallback_models: list[BaseChatModel] | None = None,
    client: DAIVSandboxClient | None = None,
    sandbox_backend: SandboxFileBackend | None = None,
) -> CompiledSubAgent:
    """
    Create the general purpose subagent for the DAIV agent.
    """
    runnable = create_agent(
        model=model,
        tools=[],
        system_prompt=_general_purpose_system_prompt(working_directory),
        middleware=_build_general_purpose_middleware(
            model,
            backend,
            runtime,
            sandbox_enabled,
            web_search_enabled,
            web_fetch_enabled,
            fallback_models,
            client,
            sandbox_backend,
        ),
        name=GENERAL_PURPOSE_NAME,
    )
    return CompiledSubAgent(name=GENERAL_PURPOSE_NAME, description=GENERAL_PURPOSE_DESCRIPTION, runnable=runnable)


def _explore_system_prompt(working_directory: str) -> str:
    root = working_directory.rstrip("/") + "/"
    return f"""\
You are a file search specialist for DAIV. You excel at thoroughly navigating and exploring codebases.

Your working directory is {root}.

=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS ===
This is a READ-ONLY exploration task. You are STRICTLY PROHIBITED from:
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
- CRITICAL: All file paths in your response MUST be absolute paths exactly as returned by the tools (e.g., {root}src/app/utils.py). Never strip prefixes or convert to relative paths — the caller uses your paths directly in tool calls.
- For clear communication, avoid using emojis
- Communicate your final report directly as a regular message - do NOT attempt to create files

NOTE: You are meant to be a fast agent that returns output as quickly as possible. In order to achieve this you must:

- Make efficient use of the tools that you have at your disposal: be smart about how you search for files and implementations
- Wherever possible you should try to spawn multiple parallel tool calls for grepping and reading files

Complete the user's search request efficiently and report your findings clearly.

{filesystem_absolute_path_directive(working_directory)}
"""  # noqa: E501


EXPLORE_SUBAGENT_DESCRIPTION = """Fast agent specialized for exploring codebases. Use this when you need to quickly find files by patterns (eg. "src/components/**/*.tsx"), search code for keywords (eg. "API endpoints"), or answer questions about the codebase (eg. "how do API endpoints work?"). When calling this agent, specify the desired thoroughness level: "quick" for basic searches, "medium" for moderate exploration, or "very thorough" for comprehensive analysis across multiple locations and naming conventions."""  # noqa: E501


# Deny rule that makes every filesystem write operation fail for the explore subagent.
# Enforced inside the deepagents filesystem tools against the validated path, so renaming
# tools upstream cannot silently restore write capability.
READ_ONLY_PERMISSIONS: list[FilesystemPermission] = [
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")
]

# Disk-mode explore permissions: read-only (deny all writes) AND fenced for reads to the three real
# /workspace subtrees plus the offloaded-artifact dirs (so the explore agent's own eviction read-back
# still works — same asymmetry as WORKSPACE_FENCE_PERMISSIONS), denying bare /workspace and any other
# path beneath it. Sandbox mode keeps plain read-only (bash is unconstrained).
EXPLORE_DISK_PERMISSIONS: list[FilesystemPermission] = [
    *READ_ONLY_PERMISSIONS,
    FilesystemPermission(
        operations=["read"], paths=[*WORKSPACE_FENCE_SUBTREES, *WORKSPACE_ARTIFACT_SUBTREES], mode="allow"
    ),
    FilesystemPermission(operations=["read"], paths=[WORKSPACE_PATH, f"{WORKSPACE_PATH}/**"], mode="deny"),
]


def _explore_permissions(*, sandbox_enabled: bool) -> list[FilesystemPermission]:
    """Read-only everywhere; additionally fence reads to the real subtrees in disk mode."""
    return READ_ONLY_PERMISSIONS if sandbox_enabled else EXPLORE_DISK_PERMISSIONS


def create_explore_subagent(
    backend: BackendProtocol, working_directory: str, *, sandbox_enabled: bool = True, **kwargs
) -> CompiledSubAgent:
    """
    Create the explore subagent.
    """
    # Local import to break a circular dependency: graph.py imports this module.
    from automation.agent.graph import dynamic_write_todos_system_prompt

    model = BaseAgent.get_model(model=site_settings.agent_explore_model_name)
    _summarization_defaults = compute_summarization_defaults(model)

    middleware: list[AgentMiddleware[Any, Any, Any]] = [
        TodoListMiddleware(system_prompt=dynamic_write_todos_system_prompt(bash_tool_enabled=False)),
        FilesystemMiddleware(
            backend=backend,
            custom_tool_descriptions=CUSTOM_TOOL_DESCRIPTIONS,
            _permissions=_explore_permissions(sandbox_enabled=sandbox_enabled),
        ),
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

    if fallback_model_name := site_settings.agent_explore_fallback_model_name:
        try:
            fallback_model = BaseAgent.get_model(model=fallback_model_name)
            middleware.append(ModelFallbackMiddleware(fallback_model))
        except Exception:
            logger.warning(
                "Could not initialize explore fallback model '%s', proceeding without fallback", fallback_model_name
            )

    runnable = create_agent(
        model=model,
        tools=[],
        system_prompt=_explore_system_prompt(working_directory),
        middleware=middleware,
        name=EXPLORE_NAME,
    )
    return CompiledSubAgent(name=EXPLORE_NAME, description=EXPLORE_SUBAGENT_DESCRIPTION, runnable=runnable)


# Names reserved for built-in subagents. Custom subagents may not use these names.
BUILTIN_SUBAGENT_NAMES: frozenset[str] = frozenset({GENERAL_PURPOSE_NAME, EXPLORE_NAME})

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

    if name in BUILTIN_SUBAGENT_NAMES:
        logger.warning("Skipping %s: name '%s' conflicts with a built-in subagent", file_path, name)
        return None

    frontmatter["name"] = name
    frontmatter["description"] = description

    body = content[match.end() :].strip()
    if not body:
        logger.warning("Skipping %s: empty body (system prompt)", file_path)
        return None

    return frontmatter, body


def _compile_subagent(
    *,
    name: str,
    description: str,
    model: BaseChatModel,
    body: str,
    middleware: list,
    working_directory: str,
    response_format: dict | type | None = None,
) -> CompiledSubAgent:
    """Compile a system-prompt body + middleware stack into a ``CompiledSubAgent``.

    Shared by ``load_custom_subagents`` (per-repo markdown subagents) and
    ``load_builtin_code_review_detectors`` (skill-shipped detector charters).
    """
    runnable = create_agent(
        model=model,
        tools=[],
        system_prompt=f"{body}\n\n{filesystem_absolute_path_directive(working_directory)}",
        middleware=middleware,
        name=name,
        response_format=response_format,
    )
    return CompiledSubAgent(name=name, description=description, runnable=runnable)


async def load_custom_subagents(
    model: BaseChatModel,
    backend: BackendProtocol,
    runtime: RuntimeCtx,
    sources: list[str],
    working_directory: str,
    sandbox_enabled: bool = True,
    web_search_enabled: bool = True,
    web_fetch_enabled: bool = True,
    fallback_models: list[BaseChatModel] | None = None,
    client: DAIVSandboxClient | None = None,
    sandbox_backend: SandboxFileBackend | None = None,
) -> list[CompiledSubAgent]:
    """
    Load custom subagents from markdown files in the given source paths.

    Each source path is scanned for .md files. Each file should contain YAML frontmatter
    with ``name`` and ``description`` fields, and a markdown body that becomes the system prompt.

    Args:
        model: The default model to use for custom subagents.
        backend: The filesystem backend.
        runtime: The runtime context.
        sources: List of paths to scan for subagent definitions.
        working_directory: The run's absolute repo root (e.g. ``/workspace/repo/``), baked into the
            subagent's filesystem path directive so it addresses files under the right root.
        sandbox_enabled: Whether to enable the sandbox middleware.
        web_search_enabled: Whether to enable web search middleware.
        web_fetch_enabled: Whether to enable web fetch middleware.
        fallback_models: Optional fallback models for model failover.

    Returns:
        List of CompiledSubAgent dicts for the loaded custom subagents.
    """
    subagents: list[CompiledSubAgent] = []

    for source_path in sources:
        try:
            result = await backend.als(source_path)
        except Exception:
            logger.debug("Could not list %s, skipping custom subagents from this source", source_path)
            continue

        md_files = [
            item["path"] for item in (result.entries or []) if not item.get("is_dir") and item["path"].endswith(".md")
        ]
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

            middleware = _build_general_purpose_middleware(
                subagent_model,
                backend,
                runtime,
                sandbox_enabled,
                web_search_enabled,
                web_fetch_enabled,
                fallback_models,
                client,
                sandbox_backend,
            )
            subagents.append(
                _compile_subagent(
                    name=frontmatter["name"],
                    description=frontmatter["description"],
                    model=subagent_model,
                    body=body,
                    middleware=middleware,
                    working_directory=working_directory,
                )
            )

            logger.info("Loaded custom subagent '%s' from %s", frontmatter["name"], file_path)

    return subagents
