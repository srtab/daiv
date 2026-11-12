---
description: "The tools the DAIV agent has, when each is available, and how it loads the ones it uses less often."
---

# Agent tools

DAIV's agent works through tools: it reads and edits files, runs commands, searches the web and reports back on your git platform. This page lists each tool, when the agent has it, and which settings turn it on or off. The agent is built on [Deep Agents](https://github.com/langchain-ai/deepagents) and runs on [LangGraph](https://langchain-ai.github.io/langgraph/).

## Tools bound up front

DAIV binds these tools to the model at the start of every run.

| Tool | What it does | When the agent has it |
|------|--------------|-----------------------|
| `ls`, `read_file`, `glob`, `grep` | List directories, read files, find files by pattern and search file contents by regular expression | Always |
| `write_file`, `edit_file` | Create a file, or change an existing one | Always |
| `bash` | Run a command in the persistent, isolated [sandbox](../features/sandbox.md), after the command policy checks it | When the run has a sandbox. [How an environment is resolved](../features/sandbox-environments.md#how-an-environment-is-resolved) says when it has one. |
| `write_todos` | Track the steps of a task | Always |
| `skill` | Run a [skill](../customization/agent-skills.md) | Always |
| `task` | Hand work to a [subagent](../features/subagents.md) | Always |
| `ask_user_question` | Ask you 1 to 4 questions and end the turn, and your answer arrives as the next message | On the main agent only, and not on a run nobody can answer: a scheduled run, a [pipeline watch](../features/pipeline-watch.md) run or a delegated job. [Answer a question](../features/sessions.md#answer-a-question) shows it from your side. |
| `delegate_jobs` | Start independent jobs on other repositories or on the same one, and resume when they finish | When [orchestration](../features/orchestration.md) is on for the repository |

## Tools loaded on demand

The agent loads these tools through `tool_search`, a tool that finds a tool by what it does and loads its definition. This keeps the list of tools the model sees short. A tool the agent loads stays available for the rest of the session. Setting `DEFERRED_TOOLS_ENABLED` to `false` binds every tool up front instead; [Deferred tools](env-variables.md#deferred-tools) describes it and its related settings.

| Tool | What it does | When the agent has it |
|------|--------------|-----------------------|
| `gitlab` | Inspect and update issues, merge requests, pipelines, job logs and releases | On a GitLab repository |
| `gh` | The same, through the `gh` command line | On a GitHub repository |
| `web_search` | Search the web through DuckDuckGo, Tavily or SearXNG | When web search is on, which is the default. [Web search](env-variables.md#web-search) describes the settings. |
| `web_fetch` | Fetch a URL, convert it to Markdown and answer a prompt about its content | When web fetch is on, which is the default. [Web fetch](env-variables.md#web-fetch) describes the settings. |
| `publish_artifact` | Copy a workspace file, such as an HTML report, a CSV or a chart, into DAIV as an [artifact](../features/sessions.md#artifacts) and return its viewer URL. With `artifact_id`, it replaces the file of an artifact the session already published. | Always |
| `fetch_artifact` | Copy an artifact the session already published back into `/workspace/tmp`, so the agent can revise it | Always |
| `check_delegated_jobs`, `cancel_delegated_jobs` | Check, or cancel, the jobs the agent delegated | When `delegate_jobs` is available |
| MCP tools | The tools of the [MCP servers](../customization/mcp-tools.md) the run has, such as Sentry or Context7 | When an MCP server is enabled for the run |

## Model priority

The agent runs on the first model that a source sets, from a model you pick for the run, through the `daiv-max` issue label and the repository's `.daiv.yml`, to the site's settings. [Model overrides](../customization/repository-config.md#model-overrides) lists the order. A subagent follows its own order, described in [Subagent model precedence](../features/subagents.md#subagent-model-precedence), and [Environment variables](env-variables.md#daiv-agent) lists the settings.
