from __future__ import annotations

from contextlib import suppress
from io import StringIO
from typing import TYPE_CHECKING, Any

from django.core.cache import cache

import yaml
from pydantic import BaseModel, Field, ValidationError, computed_field, field_validator

if TYPE_CHECKING:
    from codebase.base import Repository

CONFIGURATION_FILE_NAME = ".daiv.yml"
CONFIGURATION_CACHE_KEY_PREFIX = "repo_config:"
CONFIGURATION_CACHE_TIMEOUT = 60 * 60 * 1  # 1 hour

REPOSITORY_DESCRIPTION_MAX_LENGTH = 400
BRANCH_NAME_CONVENTION_MAX_LENGTH = 100


class Features(BaseModel):
    auto_address_review_enabled: bool = Field(default=True, description="Enable code review automation features.")
    auto_address_issues_enabled: bool = Field(default=True, description="Enable issue implementation features.")
    autofix_pipeline_enabled: bool = Field(
        default=True, description="Enable autofix of issues detected on the pipelines."
    )


class RepositoryConfig(BaseModel):
    default_branch: str | None = Field(
        default=None,
        description=(
            "Specifies the default branch DAIV should use. If not set, the repository default branch will be used."
        ),
    )
    repository_description: str = Field(
        default="",
        max_length=REPOSITORY_DESCRIPTION_MAX_LENGTH,
        description=(
            "A brief description of the repository. "
            "Include details to DAIV understand your repository, "
            "like code standadrs to follow or main technologies used."
            "This information will be used to provide better insights and recommendations."
        ),
        examples=[
            "Python based project for data analysis. Follows PEP8 standards.",
            "React based project with TypeScript. Follows Airbnb style guide.",
        ],
    )

    # Feature flags and toggles
    features: Features = Field(default_factory=Features, description="Feature flags and toggles for DAIV.")

    # Codebase restrictions
    exclude_patterns: tuple[str, ...] = Field(
        default=(
            # files
            "*pipfile.lock",
            "*package-lock.json",
            "*yarn.lock",
            "*gemfile.lock",
            "*composer.lock",
            "*uv.lock",
            "*.svg",
            # folders
            "*.git/*",
            "*.mypy_cache/*",
            "*.tox/*",
            "*vendor/*",
            "*venv/*",
            "*node_modules/*",
            "*dist/*",
        ),
        description=(
            "List of path patterns that DAIV should ignore when indexing and analyzing the codebase. "
            "For more information on the glob syntax, refer to the `pathlib` documentation: "
            "https://docs.python.org/3/library/pathlib.html#pathlib-pattern-language."
        ),
    )
    extend_exclude_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "List of path patterns that DAIV should ignore when indexing and analyzing the codebase, "
            "in addition to those specified by `exclude_patterns`."
            "To know more about the patterns you can use, check the documentation: "
            "https://docs.python.org/3/library/pathlib.html#pathlib-pattern-language."
        ),
        examples=["*tests/*", "*requirements.txt"],
    )

    # Pull request management
    branch_name_convention: str = Field(
        default="always start with 'daiv/' followed by a short description.",
        description="The convention to use when creating branch names.",
        examples=["Use 'feat/', 'fix/', or 'chore/' prefixes."],
        max_length=BRANCH_NAME_CONVENTION_MAX_LENGTH,
    )

    @field_validator("repository_description", "branch_name_convention", mode="before")
    @classmethod
    def truncate_if_too_long(cls, value: Any, info):
        max_length = None
        if info.field_name == "repository_description":
            max_length = REPOSITORY_DESCRIPTION_MAX_LENGTH
        elif info.field_name == "branch_name_convention":
            max_length = BRANCH_NAME_CONVENTION_MAX_LENGTH

        if isinstance(value, str) and len(value) > max_length:
            return value[:max_length]
        return value

    @staticmethod
    def get_config(repo_id: str, repository: Repository | None = None) -> RepositoryConfig:
        """
        Get the configuration for a repository.
        If the configuration file is not found, a default configuration is returned.
        The configuration is cached for a period of time to avoid unnecessary requests to the repository.

        Args:
            repo_id (str): The repository ID.

        Returns:
            RepositoryConfig: The configuration for the repository.
        """
        from codebase.clients import RepoClient

        cache_key = f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}"

        if (cached_config := cache.get(cache_key)) is not None:
            return RepositoryConfig(**cached_config)

        repo_client = RepoClient.create_instance()

        if repository is None:
            repository = repo_client.get_repository(repo_id)

        if config_file := repo_client.get_repository_file(
            repo_id, CONFIGURATION_FILE_NAME, ref=repository.default_branch
        ):
            with suppress(ValidationError):
                config = RepositoryConfig(**yaml.safe_load(StringIO(config_file)))
        else:
            config = RepositoryConfig()

        if not config.default_branch:
            config.default_branch = repository.default_branch

        cache.set(cache_key, config.model_dump(), CONFIGURATION_CACHE_TIMEOUT)
        return config

    @staticmethod
    def invalidate_cache(repo_id: str) -> None:
        """
        Invalidate cache for a specific repository.
        """
        cache.delete(f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}")

    @property
    @computed_field
    def combined_exclude_patterns(self) -> tuple[str, ...]:
        """
        Combines the base exclude patterns with any additional patterns specified.
        Returns a tuple of all patterns that should be excluded.
        """
        return tuple(set(self.exclude_patterns) | set(self.extend_exclude_patterns))
