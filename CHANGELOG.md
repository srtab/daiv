# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Added Agent Skills system implementing Anthropic's progressive disclosure pattern.
- Added `creating-agents-md-file` skill to help generate `AGENTS.md` files for repositories, following the [AGENTS.md](https://agents.md/) format.
- Added `maintaining-changelog` skill to help maintain changelog files for pull requests, following existing format conventions or creating new files using Keep a Changelog format.
- Added `creating-daiv-yml-config` skill to help create `.daiv.yml` configuration files with sandbox settings (base_image and format_code commands) based on repository content.
- Added chunked reading capability to `read_tool` with `start_line` and `max_lines` parameters. The tool now supports reading files in segments rather than loading entire file contents, limiting output to a default maximum of 2000 lines. When content is truncated, a message indicates the range shown and total lines available, guiding further reads. This addresses the issue of costly full-file reads for large files.
- Added `SWERepoClient` to support SWE-bench style evaluations with public OSS repositories. This client clones repositories to temporary directories without requiring credentials and is designed for automated testing scenarios.
- Added support to `gpt-5.1`, `gpt-5.1-codex`, `gpt-5.1-codex-mini` models from OpenAI.
- Added OpenRouter support to Anthropic caching middleware, reducing costs.
- Added `FileNavigationMiddleware`, `FileEditingMiddleware`, `MergeRequestMiddleware` and `WebSearchMiddleware` in replacement of toolkits, leveraging LangChain v1 middlewares capabilities to inject the system prompt and tools into the model call.
- Added `EXECUTION_THINKING_LEVEL` configuration to `PlanAndExecuteAgent` to allow users to enable thinking for execution tasks.
- Added `/clone-to-topic` quick action to clone issues to all repositories matching specified topics, enabling bulk distribution of issues across multiple repositories.

### Changed

- Changed default model for `PlanAndExecuteAgent` to `gpt-5.1` and `gpt-5.1-codex-mini` for planning fallback and code review respectively.
- Improved `PlanAndExecuteAgent` planning output to be more structured and easier to human understand.
- Improved `PlanAndExecuteAgent` planning prompts with "Code minimalism" guidelines to prevent over-engineering and unnecessary changes.
- Migrated all prompt templates from Jinja2 to Mustache format to prevent code injection attacks.
- Replaced `plan_think_tool` with `TodoListMiddleware` to allow the agent to maintain a todo list of the tasks to be completed during the planning phase.

### Fixed

- Fixed `format_code_tool` to properly apply the patch to the repository even when the command fails.
- Fixed inclusion of `.git` directory in the sandbox archive, preventing the agent from accessing the repository and reducing archive size.
- Fixed `InvalidNamespaceError` when branch names contain periods (e.g., `fix/python-version-3.11`) by sanitizing namespace labels for LangGraph store.
- Fixed `PlanAndExecuteAgent` to use `ToolStrategy` for execution tasks instead of `AutoStrategy` to handle union types.

## [1.0.0] - 2025-11-17

### Added

- Added support to `github` client type to allow users to use GitHub as the client for the codebase.
- Added to `PlanAndExecuteAgent` the capability to:
  - load images from the user message to help the agent to visualize them (using `InjectImagesMiddleware`).
  - plan and execute commands using the DAIV Sandbox tools. This will allow the agent to perform actions on the codebase, such as installing/updating dependencies ensuring lock files are updated, generating translations, etc.
  - load the repository instructions from a `AGENTS.md` file, which is a markdown file that follows the [AGENTS.md](https://agents.md/) format.
  - fix pipelines by retrieving the pipeline status and job logs when planning using the new `pipeline` and `job_logs` tools.
  - review code changes against the plan tasks using the new `review_code_changes` tool ensuring the changes are correct and complete.
- Added `InjectImagesMiddleware` LangChain v1 middleware to automatically extract and process images from markdown/HTML syntax in user messages, supporting GitHub user-attachments, GitLab uploads, and external URLs.
- Added `AGENTS.md` file to the project.
- Added `omit_content_patterns` to DAIV configuration to allow users to omit files content, but visible for the agents (the agent will only be able to see that the file exists, but not its content).
- Added evaluation tests for `CodebaseChatAgent`, `PullRequestDescriberAgent` and `PlanAndExecuteAgent`.
- Added support to `gpt-5`, `gpt-5-nano`, `gpt-5-mini` and `gpt-5-codex` models from OpenAI.
- Added support to `grok-code-fast-1` model from Grok.
- Added support to `claude-sonnet-4.5`, `claude-opus-4.1` and `claude-haiku-4.5` models from Anthropic.
- Added support to `deepseek-v3.1-terminus` model from DeepSeek.
- Added support to `glm-4.6` model from Z-AI.
- Added support to `qwen3-max` and `qwen3-coder-plus` models from Qwen.
- Added support to `kimi-k2-thinking` model from MoonshotAI.
- Added `RECURSION_LIMIT` configuration to `CodebaseChatAgent` to allow users to change the limit of recursive calls to the agent.
- Added support to delete entire directories with the `delete` tool.

### Changed

- Fixed async Celery tasks to properly handle Django connection pooling by implementing `ThreadSensitiveContext` wrapper and worker process signal handlers, preventing connection pool exhaustion.
- Migrated LangChain and LangGraph to v1.x with updated imports and API patterns.
- Improved planning prompt of `PlanAndExecuteAgent` to deal better with asking for clarification, ensuring the agent will ask questions contextualized to the current state of the codebase.
- Changed `CodebaseChatAgent` to only be able to answer questions about a repository at a time by passing the repository id as a header. This is direct consequence of removing codebase indexation, making it difficult to answer questions about multiple repositories at the same time. **BREAKING CHANGE**
- Changed `PullRequestDescriberAgent` to use diffs to describe the changes instead of commit messages, making it more accurate and concise.
- Replaced repository read tools `search_code_snippets`, `retrieve_file_content`, and `repository_structure` with the new `glob`, `grep`, `ls`, and `read` tools.
- Replaced repository write tools `create_new_repository_file`, `replace_snippet_in_file`, `rename_repository_file` and `delete_repository_file` with the new `write`, `edit`, `delete` and `rename` tools.
- Replaced sandbox tools `run_sandbox_commands` and `run_sandbox_code` with the new `bash` tool.
- Replaced LLM-based image extraction in `PlanAndExecuteAgent` with regex-based utility function for improved performance and reduced costs.
- Migrated in-memory store based file changes to actual filesystem based file changes and commits tracking using GitPython.
- Migrated default database from `pgvector/pgvector:pg17` to `postgres:17.6`.
- Migrated project from Python 3.13 to Python 3.14.
- Refactored repository configuration file schema to be more flexible and easier to use. **BREAKING CHANGE**
- Moved tools from `daiv/automation/tools` to `daiv/automation/agents/tools`.
- Moved quick actions from `daiv/automation/quick_actions` to `daiv/quick_actions`.
- Migrated quick action `help` to activate as `@daiv /help` instead of `@daiv help`. **BREAKING CHANGE**
- Migrated quick action `plan execute` to activate as `@daiv /approve-plan` instead of `@daiv plan execute`. **BREAKING CHANGE**
- Migrated quick action `plan revise` to activate as `@daiv /revise-plan` instead of `@daiv plan revise`. **BREAKING CHANGE**
- Updated project dependencies.
- Updated documentation.

### Fixed

- Current date time format is now excluded hours and minutes, making prompts cacheable.
- Blocked GitLab and GitHub callbacks if client type is not set to the corresponding client.
- Fixed `PlanAndExecuteAgent` to avoid reading the same files twice before executing the planned changes.
- Fixed `write` and `rename` tools to create parent directories automatically when they don't exist, preventing `FileNotFoundError`.
- Fixed sandbox session management to properly reuse sessions across multiple `bash` tool invocations by replacing ContextVar-based storage with LangGraph store-based persistence.
- Fixed DuckDuckGo search tool to use the new `ddgs` package instead of the deprecated `duckduckgo-search` package.

### Removed

- Removed codebase indexation feature in favor of the new navigation tools.
- Removed `CodeDescriberAgent`.
- Removed `CodebaseSearchAgent`.
- Removed `ImageURLExtractorAgent`.
- Removed `SnippetReplacerAgent`.
- Removed `RunSandboxCodeTool`.
- Removed `IssueAddressorAgent` (replaced by `PlanAndExecuteAgent`).
- Removed `PipelineFixerAgent` (replaced by `ReviewAddressorAgent` + `pipeline` and `job_logs` tools).
- Removed all notebooks from the project.
- Removed support to `claude-sonnet-4` and `claude-opus-4` models from Anthropic.
- Removed support to `deepseek-chat-v3.1` model from DeepSeek.
- Removed support to `o4-mini` model from OpenAI.

## [0.3.0] - 2025-07-25

### Added

- Added quick actions feature to allow users perform actions by commenting on the merge request or issue.
- Added quick actions to allow users to trigger plan revision by commenting `@daiv plan revise` on the issue.

### Changed

- Migrated `RunSandboxCommandsTool` and `RunSandboxCodeTool` to be async only.
- Migrated `PipelineFixerAgent` to be triggered by a quick action instead of a webhook, allowing users to request a repair plan to fix pipelines by commenting `@daiv pipeline repair` on the merge request.
- Migrated `IssueAddressorAgent` plan approval to be triggered by a quick action, allowing users to request a plan approval by commenting `@daiv plan execute` on the issue.

### Fixed

- `ReviewAddressorAgent` was not handling single line notes without line range, leading to empty diff content.
- `IssueAddressorAgent` was not handling correctly issues with the bot label on the title, leading to errors. Now it will remove the bot label from the title. #435
- Mentions to the bot on the review comments were not being handled correctly, leading the agent to ask for clarification about who is being mentioned in this context. #436

## [0.2.1] - 2025-06-17

### Added

- Added support to `o3` model from OpenAI.
- Added build and push docker image to `main` branch to allow testing edge versions of the project.

### Changed

- Changed default model for `CodebaseChatAgent` to `gpt-4.1`.
- Changed `PlanAndExecuteAgent` planning phase to use `medium` thinking level by default.
- Updated deployment documentation to include information about the MCP proxy.
- Improved `PipelineFixerAgent` to ensure the `troubleshoot_analysis_result` (renamed to `complete_task`) tool is called exactly once at the end of the workflow.
- Improved `PipelineFixerAgent` troubleshooting details to include more context about the issue.
- `ReviewAddressorAgent` now will only accept reviews for merge requests that have DAIV mentions on the discussion thread.
- Updated MCP `@sentry/mcp-server` to `0.12.0` version.
- Updated base python image to `3.13.5`.

### Fixed

- `MCPServer.get_connection` now attaches an `Authorization: Bearer` header when `MCP_PROXY_AUTH_TOKEN` secret is configured, ensuring authenticated requests to the MCP proxy. (#419)

### Removed

- Support to `claude-3-7-sonnet` model from Anthropic.

## [0.2.0] - 2025-06-09

### Added

- Added `author` to metadata on `ReviewAddressorAgent` and `IssueAddressorAgent` to track the agent executions on the `langsmith` platform.
- Added MCP tools support to allow the agent to use external tools through MCP servers: #274.

### Changed

- Improved `PlanAndExecuteAgent`:
  - Completely rewrote planning system prompt to be more structured and concise with clear workflow steps and rules of thumb.
  - Enhanced execution system prompt with better organization and clearer instructions for applying change plans.
  - Simplified plan template format for better readability and reduced verbosity.
  - Enhanced tools schema docstrings with more detailed field descriptions and usage guidelines.
  - These improvements affect all agents that use `PlanAndExecuteAgent`: `ReviewAddressorAgent`, `IssueAddressorAgent`, and `PipelineFixerAgent`.
- Improved `ReviewAddressorAgent`:
  - Completely rewrote reviewer response prompt with structured workflow steps, better context handling, and improved reasoning with the `think` tool.
  - Enhanced review planning prompt with clear workflow steps, better diff handling guidance, and structured reasoning process.
  - Improved prompt organization with visual separators and clearer section headers for better readability.
- Improved `PipelineFixerAgent`:
  - Completely rewrote troubleshooting system prompt to be more structured and concise with clear workflow steps and rules of thumb.
  - Enhanced troubleshooting human prompt with better context handling.
  - Simplified troubleshooting template format for better readability and reduced verbosity.
- Migrated project to be async by default.
- Updated project dependencies.

### Fixed

- When changing the state of an Issue (from `closed` to `opened`), the webhook was being ignored by the GitLab callback.
- Planning questions on Issue Addressor Agent were not being handled correctly.
- Recursion limit was not being correctly passed to `PlanAndExecuteAgent`, limiting the agent to only 25 calls.

## [0.1.5] - 2025-05-26

### Added

- Added `cleanup_indexes` command to clean up outdated indexes and inaccessible repositories.

### Fixed

- Fixed connection closed or lost on `ConnectionPool` by using `check_connection` to verify if the connection is still working.
- Fixed chunks length check to use the correct number of tokens instead of the number of characters.

## [0.1.4] - 2025-05-22

### Added

- Added support to `claude-sonnet-4` and `claude-opus-4` models from Anthropic.

### Changed

- Optimized `PullRequestDescriberAgent` prompt to improve the quality of the responses for a 0-shot agent.
- Optimized `CodebaseChatAgent` prompts to improve the quality of the responses, reduce hallucinations, gatekeeping first and improve the reasoning capabilities of the agent.
- Updated `PlanAndExecuteAgent` to use `claude-sonnet-4` as the default model for planning and execution.

### Fixed

- `reply_reviewer` node of `ReviewAddressorAgent` was not using the correct tool to reply to the reviewer comments. We completely refactored the agent to turn it more reliable and robust.
- `SearchCodeSnippetsTool` was being called with `repository` parameter even when `repo_id` was being provided, leading to errors. Now we support conditionally add the `repository` parameter to the signature of the tool.
- Sometimes `Document.id` was being defined as an uuid when retrieving the document from the database, leading to errors..

## [0.1.3] - 2025-05-20

### Added

- Added mapping for `yaml` language for `.yaml` and `.yml` extensions.

### Fixed

- Temperature is being sent on `o4-mini` model, which is not supported.
- Large chunks were being indexed, causing errors on the embedding process. Now it will skip chunks that are too large (more than 2x the chunk size). #378

## [0.1.2] - 2025-05-15

### Added

- Added support to `04-mini` model from OpenAI.
- Added support to define `LANGSMITH_API_KEY` as docker secrets.

### Changed

- Improved plan comment template readability by adding a separator between the each step of the plan.
- Normalized `WEB_SEARCH_API_KEY` to be `AUTOMATION_WEB_SEARCH_API_KEY` and followed the same pattern for other keys.

### Fixed

- Fixed `ImportError` when `LanguageParser` try to parse a files with `tree-sitter-languages`, which is not installed.
- Fixed system checks to verify if required environment variables or docker secrets are set up.
- Fixed `PushCallback` to consider only merge requests created by DAIV to avoid indexing every merge request on the project.

## [0.1.1] - 2025-05-14

### Fixed

- Fixed `start-app` script passing iligal option `-o`.

## [0.1.0] - 2025-05-13

### Added

- Added security check to the GitLab callback to validate the `X-Gitlab-Token` header: #93.
- Added posibility to configure `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `WEB_SEARCH_API_KEY` and `EMBEDDINGS_API_KEY` using docker secrets.

### Changed

- Improved how to set up a test project on local GitLab in the README.md file.
- Replaced `gpt-4o` and `gpt-4o-mini` with the new OpenAI models `gpt-4.1` and `gpt-4.1-mini`.
- Replaced `o3-mini` with the new reasoning OpenAI model `o4-mini`.
- Replaced `gemini-2.0-flash` and `gemini-2.0-flash-lite-001` with `gpt-4.1-mini` and `gpt-4.1-nano` respectively.
- Simplified `CodebaseChatAgent` and `PullRequestDescriberAgent` prompts to make the agent job—and the prompt reader's job—simpler and less error‑prone.
- Migrated all evaluators/assessments logics to standalone agents to allow testing and customizing them independently from the main agents.
- Parallelized `update_index` process to improve performance.
- Improved codebase chunking process by replacing `RecursiveCharacterTextSplitter` and integrating more specialized splitters for Markdown and all languages supported by tree-sitter-language-pack using Chonkie package. `RecursiveCharacterTextSplitter` is now used as a fallback splitter.
- Added Roadmap section to the README.md.
- Updated project urls declared in `pyproject.toml` to use standard labels.
- Updated sensible `pydantic` settings to use `SecretStr` to avoid exposing sensitive information.

### Fixed

- Turned Sandbox tools more resilient and prevent failing the whole agent execution when the sandbox is unavailable.
- Empty repositories case was not being considered on the repository structure tool, causing an not found error.
- Repository index was not updating the `sha` field on the `CodebaseIndex` model, causing the index to be considered as outdated even when it's not.

### Removed

- Removed dependency on `gunicorn` and used `uvicorn` as the default server.


[Unreleased]: https://github.com/srtab/daiv/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/srtab/daiv/compare/v0.3.0...v1.0.0
[0.3.0]: https://github.com/srtab/daiv/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/srtab/daiv/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/srtab/daiv/compare/v0.1.5...v0.2.0
[0.1.5]: https://github.com/srtab/daiv/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/srtab/daiv/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/srtab/daiv/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/srtab/daiv/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/srtab/daiv/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/srtab/daiv/releases/tag/v0.1.0
