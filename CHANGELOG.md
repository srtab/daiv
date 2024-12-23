# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

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

[Unreleased]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.13...HEAD
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
