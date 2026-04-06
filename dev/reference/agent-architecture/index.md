# Agent Architecture

DAIV uses a single AI agent built on [Deep Agents](https://github.com/langchain-ai/deepagents), a general-purpose deep agent framework from [LangChain](https://python.langchain.com/) with sub-agent spawning, middleware-based architecture, and virtual filesystem — all running on [LangGraph](https://langchain-ai.github.io/langgraph/). This page covers the technical architecture for those who want to understand how DAIV works under the hood.

## Overview

DAIV's architecture consists of:

- **One main agent** — handles all tasks (issue addressing, code review, slash commands)
- **Two subagents** — general-purpose (full tools) and explore (read-only, fast)
- **Middleware stack** — modular capabilities injected based on configuration
- **MCP servers** — external tool integrations (Sentry, Context7)

```
graph TB
    WH[Webhook Event] --> CB[Callback Handler]
    CB --> TQ[Task Queue]
    JOB[Jobs API] --> TQ
    TQ --> MGR[Manager / Task]
    MGR --> AGENT[DAIV Agent]

    AGENT --> MW[Middleware Stack]
    MW --> FS[Filesystem Tools]
    MW --> GIT[Git Tools]
    MW --> GP[Git Platform Tools]
    MW --> SB[Sandbox]
    MW --> WS[Web Search / Fetch]
    MW --> SK[Skills]
    MW --> SA[Subagents]
    MW --> MCP[MCP Tools]

    SA --> GPAgent[General-Purpose]
    SA --> EXAgent[Explore]

    AGENT --> PUB[Git Change Publisher]
    PUB --> COMMIT[Commit & Push]
    PUB --> MR[Create / Update MR]
```

## End-to-end flow

1. **Trigger** — a webhook event from GitLab/GitHub, or a [Jobs API](https://srtab.github.io/daiv/dev/features/jobs-api/index.md) request
1. **Dispatch** — the callback handler (webhooks) or API view (jobs) enqueues a background task
1. **Context setup** — the task sets up the runtime context (repository, branch, scope) and creates the agent
1. **Agent execution** — LangGraph runs the agent loop: call LLM → execute tools → repeat
1. **Output** — the agent commits changes and creates/updates a merge request (webhooks), or the text result is stored for polling (jobs)

### Managers

Two managers orchestrate the agent:

| Manager                    | Trigger                 | Purpose                              |
| -------------------------- | ----------------------- | ------------------------------------ |
| `IssueAddressorManager`    | Issue with `daiv` label | Plans and implements issue solutions |
| `CommentsAddressorManager` | `@daiv` mention on MR   | Responds to code review comments     |

Both create a persistent conversation thread (stored in Redis with 90-day TTL) so the agent retains context across multiple interactions on the same issue or MR.

## Tools

The agent's tools are injected via middlewares. Each middleware provides one or more tools and can be conditionally enabled.

### Filesystem

| Tool         | Description                     |
| ------------ | ------------------------------- |
| `glob`       | Find files by pattern matching  |
| `grep`       | Search file contents with regex |
| `read_file`  | Read file contents              |
| `edit_file`  | Modify existing files           |
| `write_file` | Create new files                |
| `ls`         | List directory contents         |

### Git platform

| Tool                | Description                                                   |
| ------------------- | ------------------------------------------------------------- |
| `gitlab` / `github` | Inspect issues, merge requests, pipeline status, and job logs |

### Sandbox

| Tool   | Description                                                 |
| ------ | ----------------------------------------------------------- |
| `bash` | Execute commands in a persistent, isolated Docker container |

Commands are evaluated against a [command policy](https://srtab.github.io/daiv/dev/features/sandbox/index.md) before execution. See [Sandbox](https://srtab.github.io/daiv/dev/features/sandbox/index.md) for details.

### Web

| Tool         | Description                                                             |
| ------------ | ----------------------------------------------------------------------- |
| `web_search` | Search the web (DuckDuckGo or Tavily)                                   |
| `web_fetch`  | Fetch a URL, convert to markdown, and answer a prompt about its content |

### Skills

| Tool    | Description                                                                                             |
| ------- | ------------------------------------------------------------------------------------------------------- |
| `skill` | Execute a [skill](https://srtab.github.io/daiv/dev/customization/agent-skills/index.md) (slash command) |

### MCP

External tools provided via [MCP servers](https://srtab.github.io/daiv/dev/customization/mcp-tools/index.md) (Sentry error tracking, Context7 documentation lookup).

## Middlewares

Middlewares are the backbone of the agent — they inject tools, system prompts, and lifecycle hooks. The agent is assembled dynamically based on which middlewares are enabled.

### Always enabled

| Middleware                         | Purpose                                                |
| ---------------------------------- | ------------------------------------------------------ |
| `FilesystemMiddleware`             | File operations (glob, grep, read, edit, write)        |
| `GitMiddleware`                    | Branch management, auto-commit, MR creation            |
| `GitPlatformMiddleware`            | Git platform CLI tool (issues, MRs, pipelines)         |
| `SkillsMiddleware`                 | Skill loading and slash command execution              |
| `SubAgentMiddleware`               | Delegates tasks to subagents                           |
| `MemoryMiddleware`                 | Loads `AGENTS.md` and repository context               |
| `TodoListMiddleware`               | Task tracking within conversations                     |
| `SummarizationMiddleware`          | Compresses conversation history when it grows too long |
| `AnthropicPromptCachingMiddleware` | Prompt caching for Anthropic models                    |
| `ToolCallLoggingMiddleware`        | Logs all tool calls                                    |
| `PatchToolCallsMiddleware`         | Fixes malformed tool calls from the LLM                |

### Conditionally enabled

| Middleware                 | Condition                                                |
| -------------------------- | -------------------------------------------------------- |
| `SandboxMiddleware`        | Sandbox is configured (`DAIV_SANDBOX_BASE_IMAGE` is set) |
| `WebSearchMiddleware`      | `DAIV_WEB_SEARCH_ENABLED` is `true`                      |
| `WebFetchMiddleware`       | `DAIV_WEB_FETCH_ENABLED` is `true`                       |
| `ModelFallbackMiddleware`  | A fallback model is configured                           |
| `HumanInTheLoopMiddleware` | Plan approval is required (non-auto mode)                |

## Subagents

The main agent can delegate work to two subagents. See [Subagents](https://srtab.github.io/daiv/dev/features/subagents/index.md) for the user-facing explanation.

| Subagent        | Model                   | Tools                | Use case                                     |
| --------------- | ----------------------- | -------------------- | -------------------------------------------- |
| General-purpose | Same as main agent      | Full tool access     | Complex searches, multi-step research        |
| Explore         | Claude Haiku 4.5 (fast) | Read-only filesystem | Quick file lookups, code structure questions |

## Dynamic system prompt

The agent's system prompt is assembled at runtime and includes:

- Current date
- Bot username
- Repository URL and git platform
- Current branch and default branch
- Available tools and their descriptions
- Loaded skill metadata
- `AGENTS.md` content (if present in the repository)

This ensures the agent always has up-to-date context about the repository it's working in.

## Model configuration

Models are resolved at three levels (highest priority first):

1. **Issue labels** — `daiv-max` switches to a stronger model with higher thinking
1. **Repository config** — `.daiv.yml` [model overrides](https://srtab.github.io/daiv/dev/customization/repository-config/#model-overrides)
1. **Environment variables** — global defaults (`DAIV_AGENT_*`)

See [Environment Variables](https://srtab.github.io/daiv/dev/reference/env-variables/#daiv-agent) for all agent model settings.
