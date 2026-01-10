from typing import TYPE_CHECKING

from deepagents.graph import SubAgent

from automation.agent.middlewares.file_system import FilesystemMiddleware
from automation.agent.middlewares.sandbox import SandboxMiddleware
from automation.agent.middlewares.web_search import WebSearchMiddleware
from codebase.utils import GitManager, redact_diff_content

if TYPE_CHECKING:
    from deepagents.backends import BackendProtocol

    from codebase.context import RuntimeCtx

GENERAL_PURPOSE_DESCRIPTION = "General-purpose agent for researching complex questions, searching for code, and executing multi-step tasks. When you are searching for a keyword or file and are not confident that you will find the right match in the first few tries use this agent to perform the search for you. This agent has access to all tools as the main agent."  # noqa: E501

GENERAL_PURPOSE_SYSTEM_PROMPT = """You are an agent for DAIV. Given the user's message, you should use the tools available to complete the task. Do exactly what has been asked. When you complete the task respond with a detailed writeup.

- For file searches: Use Grep or Glob when you need to search broadly. Use Read when you know the specific file path.
- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested.
- Any file paths you return in your response MUST be absolute. Do NOT use relative paths.
"""  # noqa: E501


EXPLORE_SYSTEM_PROMPT = """\
You are a file search specialist for DAIV. You excel at thoroughly navigating and exploring codebases.

=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS === This is a READ-ONLY exploration task. You are STRICTLY PROHIBITED from:
- Creating new files (no Write, touch, or file creation of any kind)
- Modifying existing files (no Edit operations)
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
- Use `read` when you know the specific file path you need to read
- Adapt your search approach based on the thoroughness level specified by the caller
- Return file paths as absolute paths in your final response
- For clear communication, avoid using emojis
- Communicate your final report directly as a regular message - do NOT attempt to create files

NOTE: You are meant to be a fast agent that returns output as quickly as possible. In order to achieve this you must:

- Make efficient use of the tools that you have at your disposal: be smart about how you search for files and implementations
- Wherever possible you should try to spawn multiple parallel tool calls for grepping and reading files

Complete the user's search request efficiently and report your findings clearly."""  # noqa: E501

EXPLORE_SUBAGENT_DESCRIPTION = """Fast agent specialized for exploring codebases. Use this when you need to quickly find files by patterns (eg. "src/components/**/*.tsx"), search code for keywords (eg. "API endpoints"), or answer questions about the codebase (eg. "how do API endpoints work?"). When calling this agent, specify the desired thoroughness level: "quick" for basic searches, "medium" for moderate exploration, or "very thorough" for comprehensive analysis across multiple locations and naming conventions."""  # noqa: E501


CHANGELOG_SYSTEM_PROMPT = """\
You are a changelog specialist for DAIV. You excel at finding, understanding, and updating changelog files in any format or location. A diff is provided to you, use it to understand the changes that have been made to the codebase.

Your mission: Maintain high-quality changelogs that help users understand what changed in a project.

=== DISCOVERY ===
Changelog files can have many names and locations. Use `ls` and/or `glob` to list and search for common patterns. Start by listing all files in the repository root, typically changelog files are located there.

=== FORMAT DETECTION ===
Analyze the existing changelog file to understand its conventions.

PRESERVE ALL DETECTED CONVENTIONS when making changes, match the existing changelog file style exactly.

=== UNRELEASED SECTION RULE ===
**CRITICAL**: Only modify the unreleased/upcoming section:
- If no unreleased section exists, create one at the top (after any header/intro)
- NEVER modify released/versioned entries - these are historical records.
- NEVER add entries to past versions.

=== ENTRY WRITING GUIDELINES ===
Write entries that help users understand changes:
- Write for end users, not developers
- Use imperative mood ("Add feature X" not "Added feature X")
- Be concise and specific
- Reference issue/PR numbers when relevant: (#123)
- Group related changes under one entry when appropriate
- Avoid duplicate entries
- One entry per logical change

=== QUALITY GUARDRAILS ===
- Match existing format exactly (categories, style, structure)
- Keep entries concise (one line when possible)
- Use consistent terminology with existing entries
- Verify changes are limited to unreleased section
- Ensure entries are user-facing and meaningful
- Avoid technical jargon when possible

=== WORKFLOW ===
1. Search for changelog files using `ls` and/or `glob` patterns
2. Read and analyze the most canonical changelog file
3. Detect format and conventions
4. Locate or create unreleased section
5. Check for existing entries about the current change
6. Add, update, or merge entries as needed

Complete the user's changelog request efficiently and report your changes clearly.

=== DIFF ===
{diff}
"""  # noqa: E501

CHANGELOG_SUBAGENT_DESCRIPTION = """Specialized agent for updating changelogs and release notes based on changes made to the codebase. Use PROACTIVELY when you need to update/edit the changelog, or when the user requests changelog updates. You will be provided with a diff of the changes that have been made to the codebase. Make sure to call this agent after all changes have been applied. Avoid editing directly the changelog if it is not explicitly requested by the user, call this agent instead."""  # noqa: E501


def create_general_purpose_subagent(backend: BackendProtocol, runtime: RuntimeCtx, offline: bool = False) -> SubAgent:
    """
    Create the general purpose subagent for the DAIV agent.
    """
    middleware = [FilesystemMiddleware(backend=backend)]

    if not offline:
        middleware.append(WebSearchMiddleware())

    if runtime.config.sandbox.enabled:
        middleware.append(SandboxMiddleware(close_session=False))

    return SubAgent(
        name="general-purpose",
        description=GENERAL_PURPOSE_DESCRIPTION,
        system_prompt=GENERAL_PURPOSE_SYSTEM_PROMPT,
        middleware=middleware,
    )


def create_explore_subagent(backend: BackendProtocol, runtime: RuntimeCtx) -> SubAgent:
    """
    Create the explore subagent.
    """
    middleware = [FilesystemMiddleware(backend=backend, read_only=True)]

    return SubAgent(
        name="explore",
        description=EXPLORE_SUBAGENT_DESCRIPTION,
        system_prompt=EXPLORE_SYSTEM_PROMPT,
        middleware=middleware,
    )


def create_changelog_subagent(backend: BackendProtocol, runtime: RuntimeCtx, offline: bool = False) -> SubAgent:
    """
    Create the changelog subagent.
    """
    git_manager = GitManager(runtime.repo)
    diff = git_manager.get_diff()

    redacted_diff = redact_diff_content(diff, runtime.config.omit_content_patterns)

    middleware = [FilesystemMiddleware(backend=backend)]

    if not offline:
        middleware.append(WebSearchMiddleware())

    return SubAgent(
        name="changelog",
        description=CHANGELOG_SUBAGENT_DESCRIPTION,
        system_prompt=CHANGELOG_SYSTEM_PROMPT.format(diff=redacted_diff),
        middleware=middleware,
    )
