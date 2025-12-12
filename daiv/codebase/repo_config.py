from __future__ import annotations

import logging
from io import StringIO
from typing import TYPE_CHECKING

from django.core.cache import cache

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator
from yaml.parser import ParserError

from automation.agents.base import ThinkingLevel  # noqa: TC001
from automation.agents.codebase_chat.conf import settings as codebase_chat_settings
from automation.agents.constants import ModelName  # noqa: TC001
from automation.agents.plan_and_execute.conf import settings as plan_and_execute_settings
from automation.agents.pr_describer.conf import settings as pr_describer_settings
from automation.agents.review_addressor.conf import settings as review_addressor_settings

if TYPE_CHECKING:
    from codebase.base import Repository

CONFIGURATION_FILE_NAME = ".daiv.yml"
CONFIGURATION_CACHE_KEY_PREFIX = "repo_config:"
CONFIGURATION_CACHE_TIMEOUT = 60 * 60 * 1  # 1 hour

BRANCH_NAME_CONVENTION_MAX_LENGTH = 100


logger = logging.getLogger("daiv.core")


class IssueAddressing(BaseModel):
    """
    Issue addressing configuration.
    """

    enabled: bool = Field(default=True, description="Enable issue addressing features.")


class CodeReview(BaseModel):
    """
    Code review configuration.
    """

    enabled: bool = Field(default=True, description="Enable code review features.")


class QuickActions(BaseModel):
    """
    Quick actions configuration.
    """

    enabled: bool = Field(default=True, description="Enable quick actions features.")


class PullRequest(BaseModel):
    """
    Pull request configuration.
    """

    branch_name_convention: str = Field(
        default="always start with 'daiv/' followed by a short description.",
        description="The convention to use when creating branch names.",
        examples=["Use 'feat/', 'fix/', or 'chore/' prefixes."],
        max_length=BRANCH_NAME_CONVENTION_MAX_LENGTH,
    )

    @field_validator("branch_name_convention", mode="before")
    @classmethod
    def truncate_if_too_long(cls, value: str, info):
        max_length = BRANCH_NAME_CONVENTION_MAX_LENGTH

        if isinstance(value, str) and len(value) > max_length:
            return value[:max_length]
        return value


class Sandbox(BaseModel):
    """
    Sandbox configuration.
    """

    base_image: str | None = Field(
        default=None,
        examples=["python:3.12-alpine", "node:18-alpine"],
        description="The base image to use for the sandbox to execute commands.",
    )
    format_code: list[str] | None = Field(
        default=None,
        examples=[["ruff check --fix", "ruff format"], ["npm run format", "npx prettier --write"]],
        description="Commands to be executed to format the code.",
    )

    @property
    def enabled(self) -> bool:
        """
        Check if the sandbox is enabled.
        """
        return self.base_image is not None

    @property
    def format_code_enabled(self) -> bool:
        """
        Check if the format code is enabled.
        """
        return self.enabled and self.format_code is not None


class PlanAndExecuteModelConfig(BaseModel):
    """
    Model configuration for the plan and execute agent.
    """

    planning_model: ModelName | str = Field(
        default=plan_and_execute_settings.PLANNING_MODEL_NAME,
        description=(
            "Model name for planning tasks. Overrides PLAN_AND_EXECUTE_PLANNING_MODEL_NAME environment variable."
        ),
    )
    planning_fallback_model: ModelName | str = Field(
        default=plan_and_execute_settings.PLANNING_FALLBACK_MODEL_NAME,
        description=(
            "Fallback model name for planning tasks. "
            "Overrides PLAN_AND_EXECUTE_PLANNING_FALLBACK_MODEL_NAME environment variable."
        ),
    )
    planning_thinking_level: ThinkingLevel | None = Field(
        default=plan_and_execute_settings.PLANNING_THINKING_LEVEL,
        description=(
            "Thinking level for planning tasks. "
            "Overrides PLAN_AND_EXECUTE_PLANNING_THINKING_LEVEL environment variable."
        ),
    )
    execution_model: ModelName | str = Field(
        default=plan_and_execute_settings.EXECUTION_MODEL_NAME,
        description=(
            "Model name for execution tasks. Overrides PLAN_AND_EXECUTE_EXECUTION_MODEL_NAME environment variable."
        ),
    )
    execution_fallback_model: ModelName | str = Field(
        default=plan_and_execute_settings.EXECUTION_FALLBACK_MODEL_NAME,
        description=(
            "Fallback model name for execution tasks. "
            "Overrides PLAN_AND_EXECUTE_EXECUTION_FALLBACK_MODEL_NAME environment variable."
        ),
    )
    execution_thinking_level: ThinkingLevel | None = Field(
        default=plan_and_execute_settings.EXECUTION_THINKING_LEVEL,
        description=(
            "Thinking level for execution tasks. "
            "Overrides PLAN_AND_EXECUTE_EXECUTION_THINKING_LEVEL environment variable."
        ),
    )
    code_review_model: ModelName | str = Field(
        default=plan_and_execute_settings.CODE_REVIEW_MODEL_NAME,
        description=(
            "Model name for code review tasks. Overrides PLAN_AND_EXECUTE_CODE_REVIEW_MODEL_NAME environment variable."
        ),
    )
    code_review_thinking_level: ThinkingLevel | None = Field(
        default=plan_and_execute_settings.CODE_REVIEW_THINKING_LEVEL,
        description=(
            "Thinking level for code review tasks. "
            "Overrides PLAN_AND_EXECUTE_CODE_REVIEW_THINKING_LEVEL environment variable."
        ),
    )


class ReviewAddressorModelConfig(BaseModel):
    """
    Model configuration for the review addressor agent.
    """

    review_comment_model: ModelName | str = Field(
        default=review_addressor_settings.REVIEW_COMMENT_MODEL_NAME,
        description=(
            "Model name for routing review comments. "
            "Overrides REVIEW_ADDRESSOR_REVIEW_COMMENT_MODEL_NAME environment variable."
        ),
    )
    reply_model: ModelName | str = Field(
        default=review_addressor_settings.REPLY_MODEL_NAME,
        description=(
            "Model name for replying to review comments. "
            "Overrides REVIEW_ADDRESSOR_REPLY_MODEL_NAME environment variable."
        ),
    )
    reply_temperature: float = Field(
        default=review_addressor_settings.REPLY_TEMPERATURE,
        description=(
            "Temperature for the reply model. Overrides REVIEW_ADDRESSOR_REPLY_TEMPERATURE environment variable."
        ),
    )


class CodebaseChatModelConfig(BaseModel):
    """
    Model configuration for the codebase chat agent.
    """

    model: ModelName | str = Field(
        default=codebase_chat_settings.MODEL_NAME,
        description="Model name for codebase chat. Overrides CODEBASE_CHAT_MODEL_NAME environment variable.",
    )
    temperature: float = Field(
        default=codebase_chat_settings.TEMPERATURE,
        description="Temperature for codebase chat. Overrides CODEBASE_CHAT_TEMPERATURE environment variable.",
    )


class PRDescriberModelConfig(BaseModel):
    """
    Model configuration for the PR describer agent.
    """

    model: ModelName | str = Field(
        default=pr_describer_settings.MODEL_NAME,
        description="Model name for PR description. Overrides PR_DESCRIBER_MODEL_NAME environment variable.",
    )


class Models(BaseModel):
    """
    Model configuration for all agents.
    """

    plan_and_execute: PlanAndExecuteModelConfig = Field(
        default_factory=PlanAndExecuteModelConfig, description="Configuration for the plan and execute agent."
    )
    review_addressor: ReviewAddressorModelConfig = Field(
        default_factory=ReviewAddressorModelConfig, description="Configuration for the review addressor agent."
    )
    codebase_chat: CodebaseChatModelConfig = Field(
        default_factory=CodebaseChatModelConfig, description="Configuration for the codebase chat agent."
    )
    pr_describer: PRDescriberModelConfig = Field(
        default_factory=PRDescriberModelConfig, description="Configuration for the PR describer agent."
    )


class RepositoryConfig(BaseModel):
    """
    Configuration for a repository.
    """

    default_branch: str | None = Field(
        default=None,
        description=(
            "Specifies the default branch DAIV should use. If not set, the repository default branch will be used."
        ),
    )
    context_file_name: str | None = Field(
        default="AGENTS.md", description="File name to load from the repository in the format of https://agents.md/."
    )

    # Codebase restrictions
    exclude_patterns: tuple[str, ...] = Field(
        default=(
            # files
            "*.pyc",
            "*.log",
            "*.zip",
            "*.coverage",
            # folders
            "**/.git/**",
            "**/.mypy_cache/**",
            "**/.tox/**",
            "**/vendor/**",
            "**/venv/**",
            "**/.venv/**",
            "**/.env/**",
            "**/node_modules/**",
            "**/dist/**",
            "**/__pycache__/**",
            "**/data/**",
            "**/.idea/**",
            "**/.pytest_cache/**",
            "**/.ruff_cache/**",
        ),
        description=(
            "List of path patterns that DAIV should ignore when navigating the codebase. "
            "For more information on the patterns syntax, refer to the `fnmatch` documentation: "
            "https://docs.python.org/3/library/fnmatch.html"
        ),
    )
    extend_exclude_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "List of path patterns that DAIV should ignore when navigating the codebase, "
            "in addition to those specified by `exclude_patterns`."
            "For more information on the patterns syntax, refer to the `fnmatch` documentation: "
            "https://docs.python.org/3/library/fnmatch.html"
        ),
        examples=["**/tests/**", "requirements.txt"],
    )
    omit_content_patterns: tuple[str, ...] = Field(
        default=("*package-lock.json", "*pnpm-lock.yaml", "*.lock", "*.svg"),
        description=(
            "List of path patterns that DAIV can see they exist in the repository but not their content. "
            "This is useful to avoid indexing large files that are not relevant to the codebase or that are generated. "
            "For example, large images, videos, or other media files, lock files, etc..."
            "For more information on the patterns syntax, refer to the `fnmatch` documentation: "
            "https://docs.python.org/3/library/fnmatch.html"
        ),
    )

    # Features
    quick_actions: QuickActions = Field(default_factory=QuickActions, description="Configure quick actions features.")
    code_review: CodeReview = Field(default_factory=CodeReview, description="Configure code review features.")
    issue_addressing: IssueAddressing = Field(
        default_factory=IssueAddressing, description="Configure issue addressing features."
    )
    pull_request: PullRequest = Field(default_factory=PullRequest, description="Configure pull request features.")
    sandbox: Sandbox = Field(
        default_factory=Sandbox, description="Configure the daiv-sandbox instance to be used to execute commands."
    )
    models: Models = Field(default_factory=Models, description="Configure model settings for agents.")

    @staticmethod
    def get_config(repo_id: str, *, repository: Repository | None = None, offline: bool = False) -> RepositoryConfig:
        """
        Get the configuration for a repository.
        If the configuration file is not found, a default configuration is returned.
        The configuration is cached for a period of time to avoid unnecessary requests to the repository.

        Args:
            repo_id (str): The repository ID.
            repository (Repository | None): The repository object.
            offline (bool): Whether to use the cached configuration or to fetch it from the repository.

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

        if not offline and (
            config_file := repo_client.get_repository_file(
                repo_id, CONFIGURATION_FILE_NAME, ref=repository.default_branch
            )
        ):
            try:
                config = RepositoryConfig(**yaml.safe_load(StringIO(config_file)))
            except ValidationError, ParserError:
                config = RepositoryConfig()
        else:
            config = RepositoryConfig()

        if not config.default_branch:
            config.default_branch = repository.default_branch

        cache.set(cache_key, config.model_dump(), CONFIGURATION_CACHE_TIMEOUT)
        logger.info("Cached configuration for repository %s", repo_id)
        return config

    @staticmethod
    def invalidate_cache(repo_id: str) -> None:
        """
        Invalidate cache for a specific repository.
        """
        cache.delete(f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}")
        logger.info("Invalidated cache for repository %s", repo_id)

    @property
    def combined_exclude_patterns(self) -> tuple[str, ...]:
        """
        Combines the base exclude patterns with any additional patterns specified.
        Returns a tuple of all patterns that should be excluded.
        """
        return tuple(set(self.exclude_patterns) | set(self.extend_exclude_patterns))
