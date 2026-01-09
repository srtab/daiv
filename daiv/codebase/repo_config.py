from __future__ import annotations

import logging
from io import StringIO
from typing import TYPE_CHECKING

from django.core.cache import cache

import yaml
from pydantic import BaseModel, Field, ValidationError
from yaml.parser import ParserError

from automation.agents.base import ThinkingLevel  # noqa: TC001
from automation.agents.constants import ModelName  # noqa: TC001
from automation.agents.deepagent.conf import settings as deepagent_settings
from automation.agents.pr_describer.conf import settings as pr_describer_settings

if TYPE_CHECKING:
    from codebase.base import Repository

CONFIGURATION_FILE_NAME = ".daiv.yml"
CONFIGURATION_CACHE_KEY_PREFIX = "repo_config:"
CONFIGURATION_CACHE_TIMEOUT = 60 * 60 * 1  # 1 hour


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


class Sandbox(BaseModel):
    """
    Sandbox configuration.
    """

    base_image: str | None = Field(
        default="python:3.12-alpine",
        examples=["python:3.12-alpine", "node:18-alpine"],
        description=(
            "The base image for the sandbox to allow agents to execute shell commands. "
            "Supply a custom image if you need preinstalled tooling."
            "To disable the sandbox, set this to `null`."
        ),
    )
    network_enabled: bool = Field(default=False, description="Whether to enable the network in the sandbox.")
    read_only_rootfs: bool = Field(
        default=True, description="Whether to enable the read-only root filesystem in the sandbox."
    )
    memory_bytes: int | None = Field(default=None, description="The memory limit for the sandbox.")
    cpu_time_seconds: int | None = Field(default=None, description="The CPU time limit for the sandbox.")
    cpus: str | None = Field(default=None, description="The CPU limit for the sandbox.")
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


class DAIVModelConfig(BaseModel):
    """
    Model configuration for the DAIV agent.
    """

    model: ModelName | str = Field(
        default=deepagent_settings.MODEL_NAME,
        description=("Model name for DAIV tasks. Overrides DAIV_AGENT_MODEL_NAME environment variable."),
    )
    fallback_model: ModelName | str = Field(
        default=deepagent_settings.FALLBACK_MODEL_NAME,
        description=(
            "Fallback model name for DAIV tasks. Overrides DAIV_AGENT_FALLBACK_MODEL_NAME environment variable."
        ),
    )
    thinking_level: ThinkingLevel | None = Field(
        default=deepagent_settings.THINKING_LEVEL,
        description=("Thinking level for DAIV tasks. Overrides DAIV_AGENT_THINKING_LEVEL environment variable."),
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

    daiv: DAIVModelConfig = Field(default_factory=DAIVModelConfig, description="Configuration for the DAIV agent.")
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
