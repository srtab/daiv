from typing import TYPE_CHECKING

from deepagents.graph import SubAgent

from automation.agents.middlewares.sandbox import SandboxMiddleware
from automation.agents.middlewares.web_search import WebSearchMiddleware

if TYPE_CHECKING:
    from codebase.context import RuntimeCtx

GENERAL_PURPOSE_DESCRIPTION = "General-purpose agent for researching complex questions, searching for code, and executing multi-step tasks. When you are searching for a keyword or file and are not confident that you will find the right match in the first few tries use this agent to perform the search for you. This agent has access to all tools as the main agent."  # noqa: E501

GENERAL_PURPOSE_SYSTEM_PROMPT = """You are an agent for DAIV. Given the user's message, you should use the tools available to complete the task. Do exactly what has been asked. When you complete the task respond with a detailed writeup.

- For file searches: Use Grep or Glob when you need to search broadly. Use Read when you know the specific file path.
- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested.
- Any file paths you return in your response MUST be absolute. Do NOT use relative paths.
"""  # noqa: E501


EXPLORE_SYSTEM_PROMPT = """\
You are a file search specialist for DAIV. You excel at thoroughly navigating and exploring codebases.

=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS ===
This is a READ-ONLY exploration task. You are STRICTLY PROHIBITED from:
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

Complete the user's search request efficiently and report your findings clearly."""  # noqa: E501

EXPLORE_SUBAGENT_DESCRIPTION = """Fast agent specialized for exploring codebases. Use this when you need to quickly find files by patterns (eg. "src/components/**/*.tsx"), search code for keywords (eg. "API endpoints"), or answer questions about the codebase (eg. "how do API endpoints work?"). When calling this agent, specify the desired thoroughness level: "quick" for basic searches, "medium" for moderate exploration, or "very thorough" for comprehensive analysis across multiple locations and naming conventions."""  # noqa: E501


def create_general_purpose_subagent(runtime: RuntimeCtx) -> SubAgent:
    """
    Create the general purpose subagent for the DAIV agent.
    """
    middleware = [WebSearchMiddleware()]

    if runtime.config.sandbox.enabled:
        middleware.append(SandboxMiddleware(close_session=False))

    return SubAgent(
        name="general-purpose",
        description=GENERAL_PURPOSE_DESCRIPTION,
        system_prompt=GENERAL_PURPOSE_SYSTEM_PROMPT,
        middleware=middleware,
    )


def create_explore_subagent(runtime: RuntimeCtx) -> SubAgent:
    """
    Create the explore subagent.
    """
    middleware = []

    if runtime.config.sandbox.enabled:
        middleware.append(SandboxMiddleware(close_session=False))

    return SubAgent(
        name="explore",
        description=EXPLORE_SUBAGENT_DESCRIPTION,
        system_prompt=EXPLORE_SYSTEM_PROMPT,
        middleware=middleware,
    )
