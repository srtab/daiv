from __future__ import annotations

import functools
from contextlib import suppress
from io import StringIO
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, Field, ValidationError

from codebase.clients import RepoClient

if TYPE_CHECKING:
    from codebase.base import Repository

CONFIGURATION_FILE_NAME = ".daiv.yml"


class Features(BaseModel):
    code_review_automation: bool = Field(default=True, description="Enable code review automation features.")
    issue_implementation: bool = Field(default=True, description="Enable issue implementation features.")
    pipeline_fixes: bool = Field(
        default=True, description="Enable pipeline root cause analysis and automated fixes features."
    )


class DAIVConfig(BaseModel):
    # Repository metadata
    default_branch: str | None = Field(
        default=None,
        description=(
            "Specifies the default branch DAIV should use. If not set, the repository default branch will be used."
        ),
    )
    repository_description: str = Field(
        default="",
        description=(
            "A brief description of the repository. "
            "Include details to DAIV understand you repository, "
            "like code standadrs to follow or main technologies used."
        ),
        examples=[
            "Python based project for data analysis. Follows PEP8 standards.",
            "React based project with TypeScript. Follows Airbnb style guide.",
        ],
    )

    # Feature flags and toggles
    features: Features = Field(default_factory=Features, description="Feature flags and toggles for DAIV.")

    # Codebase restrictions
    blocked_paths: list[str] = Field(
        default_factory=list,
        description="List of paths that DAIV should ignore when analyzing the codebase.",
        examples=["tests/", "docs/"],
    )
    ignored_file_types: list[str] = Field(
        default_factory=list,
        description="List of file types that DAIV should ignore when analyzing the codebase.",
        examples=[".md", ".txt"],
    )

    # Pull request management
    branch_name_convention: str = Field(
        default="always start with 'daiv/' followed by a short description.",
        description="The convention to use when creating branch names.",
        examples=["always start with 'feat/' or 'fix/' followed of short description."],
    )

    # Localization and internationalization
    primary_language: str = Field(
        default="English",
        description="The primary language that DAIV should use for messages.",
        examples=["English", "Portuguese (Portugal)"],
    )

    @staticmethod
    @functools.cache
    def from_repo(repository: Repository | None = None) -> DAIVConfig:
        """
        Load the DAIV configuration from the repository.

        Args:
            repository (Repository, optional): The repository to load the configuration from. Defaults to None.

        Returns:
            DAIVConfig: The DAIV configuration.
        """
        repo_client = RepoClient.create_instance()
        if repository and (
            config_file := repo_client.get_repository_file(
                repository.slug, CONFIGURATION_FILE_NAME, ref=repository.default_branch
            )
        ):
            yaml_content = yaml.safe_load(StringIO(config_file))

            with suppress(ValidationError):
                config = DAIVConfig(**yaml_content)
                if not config.default_branch:
                    config.default_branch = repository.default_branch
                return config
        return DAIVConfig()
