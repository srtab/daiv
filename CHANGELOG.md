# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Added `SENTRY_CA_CERTS` configuration to support Sentry communicate through SSL from custom CA.

### Changed

- Changed `IssueAddressorManager` to comment on the issue when an unexpected error occurs.

### Removed

- Removed unused `get_openai_callback` on codebase managers.

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

[Unreleased]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.8...HEAD
[0.1.0-alpha.8]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.7...v0.1.0-alpha.8
[0.1.0-alpha.7]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.6...v0.1.0-alpha.7
[0.1.0-alpha.6]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.5...v0.1.0-alpha.6
[0.1.0-alpha.5]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.4...v0.1.0-alpha.5
[0.1.0-alpha.4]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.3...v0.1.0-alpha.4
[0.1.0-alpha.3]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.2...v0.1.0-alpha.3
[0.1.0-alpha.2]: https://github.com/srtab/daiv/compare/v0.1.0-alpha.1...v0.1.0-alpha.2
[0.1.0-alpha.1]: https://github.com/srtab/daiv/compare/v0.1.0-alpha...v0.1.0-alpha.1
[0.1.0-alpha]: https://github.com/srtab/daiv/releases/tag/v0.1.0-alpha
