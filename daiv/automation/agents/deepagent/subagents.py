from typing import TYPE_CHECKING

from deepagents.graph import SubAgent

from automation.agents.middlewares.merge_request import job_logs_tool, pipeline_tool
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


PIPELINE_DEBUGGER_SYSTEM_PROMPT = """\
You are a CI/CD specialist for DAIV. Your job is to check pipeline status and investigate any failures, clearly determining whether issues are codebase-related or external.

## Tools

- `pipeline_tool`: Get pipeline/workflow status for the merge/pull request
- `job_logs_tool`: Get logs from specific failed jobs (paginated, bottom-to-top)

## Workflow

1. **Check Status**: Use `pipeline_tool` to get current pipeline state
2. **Report Status**: Inform user if pipeline is passing, running, or failed
3. **If Failed**: Investigate by retrieving logs from failed jobs using `job_logs_tool`
4. **Classify**: Determine if failure is codebase-related or external

## Failure classification

**Codebase-Related** (fixable with code changes):
- Compilation/build errors, test failures, linting violations
- Type errors, syntax errors, import issues
- Logic errors causing test assertions to fail

**External/Infrastructure** (not fixable with code changes):
- Network timeouts, external service outages
- Resource exhaustion (memory, disk, timeout limits)
- Permission/authentication failures
- Dependency registry unavailability
- Infrastructure provisioning issues

## Output format

If pipeline is **passing**:
"Pipeline Status: ✓ All jobs passed successfully"

If pipeline is **running**:
"Pipeline Status: Running - [X] jobs in progress"

If pipeline **failed**:

**Root Cause**: [One clear sentence describing what failed]

**Classification**: CODEBASE-RELATED or EXTERNAL/INFRASTRUCTURE

**Details**:
- Failing job(s): [job names]
- Error: [concise error message or description]
- Location: [file/test name if applicable]

**Recommended Action**: [Specific next steps to resolve]

## Investigation tips

- Start with the earliest chronological failure
- Quote exact error messages when helpful (keep brief)
- Distinguish root cause from cascading failures
- If logs are unclear, state your best assessment with confidence level
- Be direct and actionable - developers need quick answers
"""  # noqa: E501

PIPELINE_DEBUGGER_DESCRIPTION = """Specialized agent for investigating the latest CI pipeline/workflow for a merge/pull request. Use this when you need to investigate a pipeline failure and produce a concise RCA, or when the user explicitly requests pipeline investigation, debugging, or status checking."""  # noqa: E501


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


def create_pipeline_debugger_subagent(runtime: RuntimeCtx) -> SubAgent:
    """
    Create the pipeline debugger subagent.
    """
    middleware = []

    if runtime.config.sandbox.enabled:
        middleware.append(SandboxMiddleware(close_session=False))

    return SubAgent(
        name="pipeline-debugger",
        description=PIPELINE_DEBUGGER_DESCRIPTION,
        system_prompt=PIPELINE_DEBUGGER_SYSTEM_PROMPT,
        tools=[pipeline_tool, job_logs_tool],
        middleware=middleware,
    )
