# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Added to `PlanAndExecuteAgent` the capability to plan and execute commands using the DAIV Sandbox tools. This will allow the agent to perform actions on the codebase, such as installing/updating dependencies ensuring lock files are updated, generating translations, etc.
- Added `omit_content_patterns` to DAIV configuration to allow users to exclude files from being indexed by the codebase indexer, but visible for the agents (the agent will only be able to see that the file exists, but not its content).

### Changed

- Changed current date time format to exclude hours and minutes, making the prompt cachable, at least until next day.
- Improved planning prompt of `PlanAndExecuteAgent` to deal better with asking for clarification, ensuring the agent will ask questions contextualized to the current state of the codebase.

### Fixed

- `PlanAndExecuteAgent` was not calling `complete_with_plan` and `complete_with_clarification` tools for some models, leading to errors. Added fallback logic to format the agent response to be compatible with the expected output.

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

## [0.1.0-beta.5] - 2025-04-15

### Added

- Added support for `HuggingFace` and `VoyageAI` embeddings models.

### Changed

- Improved `README.md` to include information about how to setup a test project on local GitLab.
- Command `update_index` now has a `--semantic-augmented-context` flag to conditionally generate semantic augmented context.

### Fixed

- PlanAndExecuteAgent: Plan prompt `<file_path>` was always empty.
- Agent model configurations did not allow you to configure models that were not declared on the `ModelName` enum. Now you can pass arbitrary strings.

## [0.1.0-beta.4] - 2025-04-06

### Changed

- Increased `max_tokens` to `4096` for `Anthropic` models.
- Removed fallback logic from all agents, as it's not needed with the new `OpenRouter` integration.
- Improved `PlanAndExecuteAgent`:
  - Introduced `think` tool on both planning and execution phases to improve the reasoning capabilities of the agent, even when using models without that capability (https://www.anthropic.com/engineering/claude-think-tool).
  - Improved planning prompt to focus on the atual state of the codebase and design plans to be more self contained and actionable.
  - Improved execution prompt to focus on the execution to strictly follow the plan.
  - File paths included on the plan are now preloaded as messages on the execution to improve the speed of the execution and reduce the number of tool calls.
- Improved `ReviewAddressorAgent`:
  - Improved planning prompt to focus on the atual diff hunk without losing the context on the comments from the reviewer.
- Improved `PipelineFixerAgent`:
  - Improved troubleshooting prompt to focus on verifiable knowledge and to improve the quality of the remediation steps.

### Added

- Support for `OpenRouter` integration, unified API for LLM providers. **Breaking change: this will be the default provider from now on as it's more reliable and has more models available.**

### Fixed

- Command parameter `base_url` on `setup_webhooks` was declared required with a default value, forcing to pass it on every call. Now it's not required and will use the value of `DAIV_EXTERNAL_URL` if not provided.
- Date and time provided to prompts were defined at compilation time, leading to a fixed date and time on all prompts. Now it's defined at runtime to provide the correct date and time for each execution.
- CodebaseChat tool calls were being shown outside <thinking> tags on OpenWebUI, polluting the UI.
- Images were not being extracted from the issue description, leading to hallucinations.

## [0.1.0-beta.3] - 2025-03-23

### Added

- Added `exclude_repo_ids` to the `update_index` command to allow excluding specific repositories by slug or id.

### Changed

- Improved `CodebaseChatAgent` output of thinking process to indicate the repository name it's searching for.

### Fixed

- `CodebaseChatAgent` was not supporting async execution, leading to errors when using the chat API. Now it supports async execution.

## [0.1.0-beta.2] - 2025-03-21

### Added

- Added `CodebaseDescriberAgent` to describe the code snippets and enrich the codebase index with contextualized information. This will improve the quality of the search results and the ability of `CodebaseChatAgent` to answer questions.

### Changed

- Improved `RunSandboxCodeTool` prompt to avoid using it only to print stuff.
- An answer is now included on the `WebSearchTool` when using Tavily integration to provide a more concise answer to the user query.

### Fixed

- Fixed graph drawing of `IssueAddressorAgent`.
- Fixed error handling on `RunSandboxCodeTool` and `RunSandboxCommandsTool` to avoid raising an exception when the sandbox is unavailable causing the agent to stop.

### Removed

- Removed `search_documents` command, as it's no longer used.

### Chore

- Migrated dependabot package-ecosystem from `pip` to `uv` (still in beta at the time of writing).
- Added package-ecosystem `docker` and `github-actions` to the dependabot config to track docker images and github actions.

## [0.1.0-beta.1] - 2025-03-18

### Added

- Introduced Jupyter notebooks for `CodebaseChatAgent`, `IssueAddressorAgent`, `PipelineFixerAgent`, `PRDescriberAgent`, and `ReviewAddressorAgent` to facilitate development and testing.
- Added support to thinking models from Anthropic: `claude-3.7-sonnet` and OpenAI: `o1` and `o3-mini`.
- Added `PlanAndExecuteAgent` to streamline the creation of agents that need to plan and execute a task. This agent replaced all plan and execute flows on `IssueAddressorAgent`, `PipelineFixerAgent`, and `ReviewAddressorAgent`. The model used to plan and execute is `claude-3.7-sonnet` to leverage the reasoning capabilities of the models, improving the quality of the plans and executions.
- Included support to expanded line range for `ReviewAddressorAgent` to allow the agent to address comments on lines that are not part of the original diff.
- Added healthchecks to the docker-compose.yml file to ensure the services are running correctly and at the correct order.
- Added `CONTRIBUTING.md` file to guide developers on how to contribute to the project.

### Changed

- Improved performance of `PostgresRetriever` by simplifying the query and take advantage of the `Hnsw` index. **Breaking change: run `update_index` with `--reset-all` to update all indexes.**
- Grouped automation settings by agent to simplify setup and customization.
- Replaced `REACTAgent` with `create_react_agent` from `langgraph.prebuilt` for streamlined ReAct agents creation.
- Refactor: All agents logic have been rewritten to make use of subgraphs created with `create_react_agent` and `PlanAndExecuteAgent`, simplifying the agents logic and reuse the pattern, as it has proven to be an effective method on all sort of tasks.
- Refactor: Pipeline Fixer logic completely rewritten to improve remediation flows, including linting remediation in case of format code not being enough to fix the pipeline. The troubleshooting is now done with a thinking model `03-mini`, improving the quality of the troubleshooting and respective remediation steps.
- Refactor: Review Addressor now add more feedback by adding notes to the end user to inform the status of the discussion resolution.
- Refactor: Issue Addressor logic improved error handling to provide the end user with more contextualized error messages.
- Improved observability data sent to the `langsmith` to track all agent executions.
- Refactor: Renamed `CodebaseQAAgent` to `CodebaseChatAgent` to better reflect the agent's purpose. Now it uses a simple ReAct agent to answer questions. Improved the prompt to be more accurate and concise, and to output the thinking process of the agent.
- Improved `RetrieveFileContentTool` to allow retrieving multiple files at once, improving the performance of the agent when retrieving multiple files.
- Enabled `token-efficient-tools` on Anthropic models to reduce the number of tokens used and turn more likely parallel tool calls.

### Removed

- Removed `ErrorLogEvaluatorAgent` as its functionality is now integrated into the `PipelineFixerAgent`.
- Removed support to DeepSeek models, as the function calling capabilitity is unstable.

## [0.1.0-alpha.22] - 2025-01-30

### Added

- Added custom Django checks to ensure the API keys for the models configured are set.
- Added `REASONING` setting to the `chat` app to enable/disable reasoning tags on the chat completion. This will allow tools like `OpenWebUI` to show loader indicating the agent is thinking.
- Added `external_link` to the `CodeSnippet` tool to allow linking to the codebase file on the Repository UI.
- Added fallback models support to `BaseAgent` to ease and streamline the fallback logic on all agents.

### Changed

- Improved `README.md` to include more information about the project and how to run it locally.
- Improved `CodebaseQAAgent` logic to consider history of the conversation and have a more agentic behavior by following the ReAct pattern. The answer now will include a `References` section with links to the codebase files that were used to answer the question.
- Moved DeepSeek integration to langchain official: `langchain-deepseek-official`.
- Now `CodebaseQAAgent` will use the fallback model if the main model is not available.

### Fixed

- Filled placeholder on the `LICENSE` file with correct copyright information.
- Agents model instantiation was not overriding the model name as expected, leading to wrong model provider handling.
- `ReActAgent` was not calling the structued output on some cases, which was not the expected behavior. Now it will always call the structured output if it's defined, even if the agent is not calling the tool.
- `FileChange` is not hashable, leading to errors when storing it in a set on `ReviewAddressorManager`. Created structure to store file changes without repeating the same file changes.
- Assessment on `ReviewAddressorAgent` was not calling the structured output on some cases, leading to errors. Now it will always be called.

## [0.1.0-alpha.21] - 2025-01-24

### Added

- Codebase search now allows to configure how many results are returned by the search.
- Added support to Google GenAI and DeepSeek chat models.
- Added command to `search_documents` to search for documents on the codebase, usefull for debugging and testing.

### Changed

- Performance improvements and cleaner use of compression retrievers on `CodebaseSearchAgent`.

### Fixed

- Tantivy index volume was not pointing to the correct folder on `docker-compose.yml`.
- `ReviewAddressorManager` was committing duplicated file changes, leading to errors calling the repo client API.

### Chore

- Removed unused code on codebase search. This was left after the latest major refactor.

## [0.1.0-alpha.20] - 2025-01-19

### Upgrade Guide

- There were substancial changes on the codebase search engines, as it was rewritten to improve the quality of the search results. You will need to run command `update_index` with `reset_all=True` to update the indexes.

### Added

- Show comment on the issue when the agent is replanning the tasks.
- Repositories file paths are now being indexed too, allowing the agent to search for file paths.
- New option `reset_all` added to the `update_index` command to allow reset indexes from all branches, not only the default branch.

### Changed

- Rewritten lexical search engine to only have one index, allowing the agent to search for answers in multiple repositories at once.
- Rewritten `CodebaseSearchAgent` to improve the quality of search results by using techniques such as: generating multiple queries, rephrasing those queries and compressing documents with listwise reranking.
- Simplified `CodebaseQAAgent` to use `CodebaseSearchAgent` to search for answers instead of binding tools to the agent.
- Changed models url paths on Chat API from `api/v1/chat/models` -> `api/v1/models` to be more consistent with OpenAI API.
- Default embedding model changed to `text-embedding-3-large` to improve the quality of the search results. The dimension of stored vectors was increased from 1536 to 2000.

### Fixed

- Issues with images were not being processed correctly, leading to errors interpreting the image from the description.
- Chat API was not returning the correct response structure when not on streaming mode.
- Codebase retriever used for non-scoped indexes on `CodebaseSearchAgent` was returning duplicate documents, from different refs. Now it's filtering always by repository default branch.

### Chore

- Updated dependencies:
  - `django` from 5.1.4 to 5.1.5
  - `duckduckgo-search` from 7.2.0 to 7.2.1
  - `httpx` from 0.27.2 to 0.28.1
  - `langchain-anthropic` from 0.3.2 to 0.3.3
  - `langchain-openai` from 0.3.0 to 0.3.1
  - `langchain-text-splitters` from 0.3.4 to 0.3.5
  - `langgraph` from 0.2.61 to 0.2.64
  - `langgraph-checkpoint-postgres` from 2.0.9 to 2.0.13
  - `langsmith` from 0.2.10 to 0.2.11
  - `psycopg` from 3.2.3 to 3.2.4
  - `pyopenssl` from 24.3 to 25
  - `pydantic` from 2.10.4 to 2.10.5
  - `pytest-asyncio` from 0.25.1 to 0.25.2
  - `python-gitlab` from 5.3.0 to 5.3.1
  - `ruff` from 0.8.7 to 0.9.2
  - `sentry-sdk` from 2.19.2 to 2.20
  - `watchfiles` from 1.0.3 to 1.0.4

## [0.1.0-alpha.19] - 2025-01-10

### Fixed

- `prepare_repository_files_as_messages` was returning messages with empty tool calls when no files were found, leading to errors on the agents.

## [0.1.0-alpha.18] - 2025-01-10

### Fixed

- Authentication method was not compatible with OpenAI API for Chat API endpoints. Changed to use the same method as the rest of the API.
- Async views were not working with sync authentication methods. Created an async authentication method to fix this.

## [0.1.0-alpha.17] - 2025-01-10

### Added

- Added `commands` configuration to `.daiv.yml` to allow fix linting issues.
- Declared `AutomationSettings` on `automation` app to centralize all settings related to the automation.
- Turned web search tool max results to be configurable through `AutomationSettings`.
- Preload repository files before plan execution to reduce execution time, reducing the number of call turns with the LLM.
- Integrated Tavily as an alternative web search tool to DuckDuckGo.
- Support for API key generation to authenticate API requests.
- API key authentication added to the chat API endpoints. You will need to create an API key to interact with the chat API.

### Changed

- Raise more context on error message when `_get_unique_branch_name` reaches the maximum number of attempts.
- Add sandbox toolkit even when no commands are declared, as the tools don't depend on it.
- Migrated instantiation of messages from agents to prompts.
- Defined `get_model_provider` as a class method on `BaseAgent` to allow using it directly without instantiating the agent.

### Fixed

- Changes not being commited when `IssueAddressorAgent` finished the execution.
- Improved `PipelineFixerAgent` prompt and output schema to force the agent to call the tool to provide the tasks, sometimes the agent was not calling the tool and just returning the tasks as messages.
- `PipelineFixerAgent` was not checking if the error log was the same as the previous one before retrying the fix.

### Removed

- Constant `DEFAULT_RECURSION_LIMIT` removed in favor of `recursion_limit` on `AutomationSettings`.
- Migrated `CODING_COST_EFFICIENT_MODEL_NAME`, `CODING_PERFORMANT_MODEL_NAME`, `GENERIC_COST_EFFICIENT_MODEL_NAME`, `GENERIC_PERFORMANT_MODEL_NAME`, `PLANING_COST_EFFICIENT_MODEL_NAME`, `PLANING_PERFORMANT_MODEL_NAME` to `AutomationSettings` to turn them into configurable settings.
- Constant `EMBEDDING_MODEL_NAME` from `codebase/models.py` as it's no longer used.

### Chore

- Updated dependencies:
  - `duckduckgo-search` from 7.1.1 to 7.2.0
  - `langchain` from 0.3.13 to 0.3.14
  - `langchain-community` from 0.3.13 to 0.3.14
  - `langgraph` from 0.2.60 to 0.2.61
  - `langsmith` from 0.2.6 to 0.2.10
  - `pydantic-settings` from 2.7 to 2.7.1
  - `mypy` from 1.14 to 1.14.1
  - `pytest-asyncio` from 0.25 to 0.25.1
  - `ruff` from 0.8.4 to 0.8.6
  - `types-pyyaml` from 6.0.12.20241221 to 6.0.12.20241230

## [0.1.0-alpha.16] - 2025-01-03

### Added

- Included new step `apply_lint_fix` on `IssueAddressorAgent`, `ReviewAddressorAgent` and `PipelineFixerAgent` to apply lint fixes to the codebase after plan execution. This will improve antecipation of lint errors and avoid pipeline failures.
- `RunSandboxCommandsTool` now supports running commands on codebase with uncommitted changes.

### Changed

- Improved `ReviewAddressorManager` to commit the file changes after all discussions are resolved, avoiding multiple pipelines being created between discussions resolution.
- Streamlined file changes namespace to avoid code repetition.
- GitLab client now `retry_transient_errors=True` to be more resilient to transient errors.
- Improved assessment of request for changes on `ReviewAddressorAgent` to allow the agent to reason before responding.
- Improved response prompt for `ReviewAddressorAgent` to avoid answering like "I'll update the code to replace", as the agent is not able to update the code but only answer questions.
- Configuration of webhooks now takes a default `EXTERNAL_URL` for `--base-url` to avoid having to pass it on every call, and now configures `pipeline_events` instead of `job_events`. **You must run command `setup_webhooks` to update the webhooks on your GitLab projects.**
- Turn `PullRequestDescriberAgent` more resilient to errors by defining fallbacks to use a model from other provider.

### Fixed

- Fixed index updates to branches that don't exist anymore, like when a branch is marked to delete after a merge request is merged: #153.
- `SnippetReplacerAgent` was replacing multiple snippets when only one was expected. Now it will return an error if multiple snippets are found to instruct the llm to provide a more specific original snippet.
- `PipelineFixerAgent` was trying to fix multiple jobs from the same stage at the same time, causing multiple fixes being applied simultaneously to the same files which could lead to conflicts or a job being fixed with outdated code. Now it will fix one job at a time. #164
- Human feedback now is sent without the first note which is the bot note to the issue addressor agent.

### Removed

- `get_repository_tree` was removed from the `RepoClient` as it's no longer used.

## [0.1.0-alpha.15] - 2024-12-30

### Added

- Added `PIPELINE_FIXER_MAX_RETRY` to the `codebase.conf` module to allow configuring the maximum number of retry iterations for the pipeline fixer.

### Changed

- Improved logging on `PipelineFixerAgent` to clarify why a pipeline fix is not being applied.

### Fixed

- Fixed access to optional parameter `actions` on `result` after `PipelineFixerAgent` has been invoked.

### Chore

- Updated dependencies:
  - `duckduckgo-search` from 7.0.2 to 7.1.1
  - `ipython` from 8.30 to 8.31
  - `jinja2` from 3.1.4 to 3.1.5
  - `langgraph-checkpoint-postgres` from 2.0.8 to 2.0.9
  - `langsmith` from 0.2.4 to 0.2.6
  - `python-gitlab` from 5.2 to 5.3
  - `coverage` from 7.6.9 to 7.6.10
  - `mypy` from 1.13 to 1.14
  - `types-pyyaml` from 6.0.12.20240917 to 6.0.12.20241221

## [0.1.0-alpha.14] - 2024-12-27

### Added

- Added `SNIPPET_REPLACER_STRATEGY` and `SNIPPET_REPLACER_MODEL` to `SnippetReplacerAgent` to allow configuring the strategy and the model to be used.

### Changed

- Migrated from `django-appconf` to `pydantic-settings` for configuration management.

### Fixed

- Fixed path to `sandbox` docker service volume for local development.

### Chore

- Removed `django_celery_beat` from the project, as it's not used.
- Updated dependencies:
  - `duckduckgo-search` from 6.3.7 to 7.0.3.

## [0.1.0-alpha.13] - 2024-12-23

### Changed

- Improved prompts for `CodebaseQAAgent` to improve the quality of the answers.
- Improved prompts for `CodebaseSearchAgent` to improve the effectiveness of the search.
- Improved prompts of plan execution to focus the agent more on the execution of the plan and less on planning.

### Fixed

- Fixed `SnippetReplacerAgent` prompt to minimize placeholders like "The rest of the code here".

## [0.1.0-alpha.12] - 2024-12-20

### Fixed

- Fixed `CodebaseSearchAgent` to avoid calling index update when no repo or ref is provided.

### Changed

- Changed default `max_tokens=2048` for all `Anthropic` models to deacrese the changes of rate limiting. Only `SnippetReplacerAgent` left using a higher value.
- Improved prompts for `ReviewAddressorAgent` and `IssueAddressorManager` to avoid excessive tool calls and optimize the agent behavior.
- Changed `tool_choice` to `auto` on `REACTAgent` to improve reasoning capabilities of the agents.
- Updated dependencies:
  - `langchain` from 0.3.11 to 0.3.13
  - `langchain-anthropic` from 0.3 to 0.3.1
  - `langchain-community` from 0.3.11 to 0.3.13
  - `langchain-openai` from 0.2.12 to 0.2.14
  - `langchain-text-splitters` from 0.3.2 to 0.3.4
  - `langgraph` from 0.2.59 to 0.2.60
  - `langsmith` from 0.2.2 to 0.2.4
  - `pydantic` from 2.10.3 to 2.10.4
  - `pytest-asyncio` from 0.24 to 0.25
  - `python-gitlab` from 5.1 to 5.2
  - `uvicorn` from 0.32.1 to 0.34.0
  - `ruff` from 0.8.2 to 0.8.4

### Removed

- Removed `check_consecutive_tool_calls` from `REACTAgent` as it's no longer used.
- Removed `ExploreRepositoryPathTool` as it's no longer used.

## [0.1.0-alpha.11] - 2024-12-18

### Removed

- Removed `update_index` and `setup_webhooks` commands from the `start-app` script to avoid long startup times.
- Removed `GUNICORN_THREADS` from the `start-app` script, as it's not used by `gunicorn` with `UvicornWorker`.

### Fixed

- Fixed connections already closed being served by the pool.

## [0.1.0-alpha.10] - 2024-12-17

### Added

- Added `DEFAULT_RECURSION_LIMIT` to the `automation.constants` module and replaced all hardcoded values with it.
- Added `ErrorLogEvaluatorAgent` to evaluate if two error logs are the same error or related.

### Changed

- Changed `is_daiv` to check the label case insensitive.
- Changed `IssueAddressorManager` to comment on the issue when the agent has questions and couldn't define a plan.
- Changed `IssueAddressorManager` to present the plan within the discussion thread created when the agent has a plan, instead of creating a sub-tasks on the issue.
- Improved `issue_addressor` templates to be more user friendly and informative.
- Improved planning prompts from `IssueAddressorAgent` and `ReviewAddressorAgent` to attempt prevent looping on to many unecessary tool calls.
- Changed `PipelineFixerAgent` to use `ErrorLogEvaluatorAgent` to evaluate if can retry fixing the pipeline and avoid looping on the same error.
- Changed `MAX_RETRY_ITERATIONS` to 10 on `PipelineFixerAgent`.
- Changed `CodebaseSearchAgent` to ensure the index is updated before retrieving the documents.

### Removed

- Removed methods `create_issue_tasks`, `get_issue_tasks` and `delete_issue` to create sub-tasks within a issue on GitLab client. This is no longer needed as the agent now creates a discussion thread to track the plan and execution.

### Fixed

- Fixed Docker group ID of `sandbox` to be compatible with Ubuntu.
- Fixed `ref` argument not being used on `update_index` command.

## [0.1.0-alpha.9] - 2024-12-12

### Changed

- Changed `IssueAddressorManager` to comment on the issue when an unexpected error occurs.
- Updated dependencies:
  - `duckduckgo-search` from 6.3.7 to 6.4.1
  - `langchain` from 0.3.9 to 0.3.11
  - `langchain-community` from 0.3.9 to 0.3.11
  - `langchain-openai` from 0.2.10 to 0.2.12
  - `langgraph` from 0.2.53 to 0.2.59
  - `langgraph-checkpoint-postgres` from 2.0.7 to 2.0.8
  - `langsmith` from 0.1.147 to 0.2.2
  - `redis` from 5.2 to 5.2.1
  - `pydantic` from 2.10.2 to 2.10.3
  - `pyopenssl` from 24.2.1 to 24.3.0
  - `ruff` from 0.8.0 to 0.8.2
  - `watchfiles` from 1.0.0 to 1.0.3

### Removed

- Removed unused `get_openai_callback` on codebase managers.
- Removed unused `monitor_beat_tasks` from Sentry Celery integration.

### Fixed

- Fixed fallback model name to be used as `model` argument instead of inexistent `model_name` on ReAct agents.
- Fixed missing `assignee_id` on `RepoClient.update_or_create_merge_request` abstract method.

## [0.1.0-alpha.8] - 2024-12-11

### Added

- Added `EXPOSE 8000` to the `Dockerfile`.
- Added `CodebaseQAAgent` to answer questions about the codebase.
- Added chat completion API endpoints to allow interact with the codebase through seamless integration with external tools and services.
- Added fallback models to allow more resilient behavior when a model is not available (this happens a lot with Anthropic models).
- Added `CONN_HEALTH_CHECKS` to the `settings.py` to allow healthchecks to be performed on the database connection.

### Changed

- Renamed `PostgresRetriever` to `ScopedPostgresRetriever` to allow having a non scoped retriever for `CodebaseQAAgent`.
- Changed `PLANING_COST_EFFICIENT_MODEL_NAME` to point to `claude-3-5-sonnet-20241022`.
- Changed `GENERIC_PERFORMANT_MODEL_NAME` to point to `gpt-4o-2024-11-20`, the latest version of `gpt-4o`.
- Changed prompt for `ReviewAddressorAgent` to try limiting the number of iterations on ReAct agent.

### Fixed

- Fixed the `Dockerfile` to create the `daiv` user with the correct group and user IDs to avoid permission issues.
- Fixed the `branch_filter_strategy` to be `all_branches` if `push_events_branch_filter` is not set.
- Fixed conditional edge after reducer in `CodebaseSearchAgent`, the state where not beign updated as expected, ignoring further iterations.
- Fixed `KeyError: 'response'` on `ReviewAddressorAgent` when the agent reached the maximum number of recursion.
- Fixed connection timeout when accessing the database with Django ORM.

## [0.1.0-alpha.7] - 2024-12-07

### Added

- Added `HEALTHCHECK` to the `Dockerfile`.

### Fixed

- Fixed the `Dockerfile` to create the `daiv` user with the correct home directory defined.
- Fixed the `Dockerfile` to create the necessary directories for the application to run: `tantivy_index`, `media`, and `static`.

## [0.1.0-alpha.6] - 2024-12-06

### Removed

- Removed `update-ca-certificates` from the entrypoint script.

## [0.1.0-alpha.5] - 2024-12-06

### Added

- Added `update-ca-certificates` to the entrypoint script.

### Fixed

- Installed missing dependency `gunicorn`.

## [0.1.0-alpha.4] - 2024-12-06

### Fixed

- Fixed the access level for the maintainer role when listing repositories to only include repositories that the authenticated user is a member of.

## [0.1.0-alpha.3] - 2024-12-06

### Fixed

- Reverted `DB_URI` configuration to not include the `pool_max_lifetime` query parameter.

## [0.1.0-alpha.2] - 2024-12-06

### Fixed

- Fixed the `DAIV_SANDBOX_API_KEY` configuration to be loaded from a Docker secret.

## [0.1.0-alpha.1] - 2024-12-06

### Added

- Integrated Sentry for error monitoring.
- Added `pool_max_lifetime` to `DB_URI` for PostgreSQL connection.
- Added a health check endpoint at `/-/alive/`.

### Fixed

- Removed `CSPMiddleware`. The `django-csp` package was removed from the project and the middleware was left behind.

### Removed

- Removed `VERSION` and `BRANCH` from settings and from production `Dockerfile`.

## [0.1.0-alpha] - 2024-12-06

### Added

- Initial release of the `daiv` project.

[Unreleased]: https://github.com/srtab/daiv/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/srtab/daiv/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/srtab/daiv/compare/v0.1.5...v0.2.0
[0.1.5]: https://github.com/srtab/daiv/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/srtab/daiv/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/srtab/daiv/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/srtab/daiv/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/srtab/daiv/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/srtab/daiv/compare/v0.1.0-beta.5...v0.1.0
[0.1.0-beta.5]: https://github.com/srtab/daiv/compare/v0.1.0-beta.4...v0.1.0-beta.5
[0.1.0-beta.4]: https://github.com/srtab/daiv/compare/v0.1.0-beta.3...v0.1.0-beta.4
[0.1.0-beta.3]: https://github.com/srtab/daiv/compare/v0.1.0-beta.2...v0.1.0-beta.3
[0.1.0-beta.2]: https://github.com/srtab/daiv/compare/v0.1.0-beta.1...v0.1.0-beta.2
[0.1.0-beta.1]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.22...v0.1.0-beta.1
[0.1.0-alpha.22]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.21...v0.1.0-alpha.22
[0.1.0-alpha.21]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.20...v0.1.0-alpha.21
[0.1.0-alpha.20]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.19...v0.1.0-alpha.20
[0.1.0-alpha.19]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.18...v0.1.0-alpha.19
[0.1.0-alpha.18]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.17...v0.1.0-alpha.18
[0.1.0-alpha.17]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.16...v0.1.0-alpha.17
[0.1.0-alpha.16]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.15...v0.1.0-alpha.16
[0.1.0-alpha.15]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.14...v0.1.0-alpha.15
[0.1.0-alpha.14]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.13...v0.1.0-alpha.14
[0.1.0-alpha.13]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.12...v0.1.0-alpha.13
[0.1.0-alpha.12]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.11...v0.1.0-alpha.12
[0.1.0-alpha.11]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.10...v0.1.0-alpha.11
[0.1.0-alpha.10]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.9...v0.1.0-alpha.10
[0.1.0-alpha.9]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.8...v0.1.0-alpha.9
[0.1.0-alpha.8]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.7...v0.1.0-alpha.8
[0.1.0-alpha.7]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.6...v0.1.0-alpha.7
[0.1.0-alpha.6]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.5...v0.1.0-alpha.6
[0.1.0-alpha.5]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.4...v0.1.0-alpha.5
[0.1.0-alpha.4]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.3...v0.1.0-alpha.4
[0.1.0-alpha.3]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.2...v0.1.0-alpha.3
[0.1.0-alpha.2]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.1...v0.1.0-alpha.2
[0.1.0-alpha.1]: https://github.com/srtab/daiv/compare/v0.1.0-alpha...v0.1.0-alpha.1
[0.1.0-alpha]: https://github.com/srtab/daiv/releases/tag/v0.1.0-alpha
