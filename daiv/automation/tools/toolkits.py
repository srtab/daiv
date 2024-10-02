from __future__ import annotations

from abc import ABCMeta, abstractmethod
from typing import TYPE_CHECKING

from langchain_core.tools.base import BaseTool
from langchain_core.tools.base import BaseToolkit as LangBaseToolkit

from codebase.indexes import CodebaseIndex

from .repository import (
    AppendToFileTool,
    CreateFileTool,
    DeleteFileTool,
    RenameFileTool,
    ReplaceSnippetWithTool,
    RepositoryFileTool,
    RepositoryTreeTool,
    SearchRepositoryTool,
)

if TYPE_CHECKING:
    from codebase.base import CodebaseChanges
    from codebase.clients import AllRepoClient


class BaseToolkit(LangBaseToolkit, metaclass=ABCMeta):
    tools: list[BaseTool]

    @classmethod
    @abstractmethod
    def create_instance(
        cls, repo_client: AllRepoClient, source_repo_id: str, source_ref: str, codebase_changes: CodebaseChanges
    ) -> BaseToolkit:
        pass

    def get_tools(self) -> list[BaseTool]:
        return self.tools


class ReadRepositoryToolkit(BaseToolkit):
    """
    Toolkit for inspecting codebases.
    """

    @classmethod
    def create_instance(
        cls, repo_client: AllRepoClient, source_repo_id: str, source_ref: str, codebase_changes: CodebaseChanges
    ) -> BaseToolkit:
        return cls(
            tools=[
                SearchRepositoryTool(source_repo_id=source_repo_id, api_wrapper=CodebaseIndex(repo_client=repo_client)),
                RepositoryFileTool(
                    source_repo_id=source_repo_id,
                    source_ref=source_ref,
                    codebase_changes=codebase_changes,
                    api_wrapper=repo_client,
                ),
                RepositoryTreeTool(
                    source_repo_id=source_repo_id,
                    source_ref=source_ref,
                    codebase_changes=codebase_changes,
                    api_wrapper=repo_client,
                ),
            ]
        )


class WriteRepositoryToolkit(ReadRepositoryToolkit):
    """
    Toolkit for modifying codebases.
    """

    tools: list[BaseTool]

    @classmethod
    def create_instance(
        cls, repo_client: AllRepoClient, source_repo_id: str, source_ref: str, codebase_changes: CodebaseChanges
    ) -> BaseToolkit:
        super_instance = super().create_instance(
            repo_client=repo_client,
            source_repo_id=source_repo_id,
            source_ref=source_ref,
            codebase_changes=codebase_changes,
        )
        super_instance.tools.extend([
            ReplaceSnippetWithTool(
                source_repo_id=source_repo_id,
                source_ref=source_ref,
                codebase_changes=codebase_changes,
                api_wrapper=repo_client,
            ),
            CreateFileTool(
                source_repo_id=source_repo_id,
                source_ref=source_ref,
                codebase_changes=codebase_changes,
                api_wrapper=repo_client,
            ),
            RenameFileTool(
                source_repo_id=source_repo_id,
                source_ref=source_ref,
                codebase_changes=codebase_changes,
                api_wrapper=repo_client,
            ),
            DeleteFileTool(
                source_repo_id=source_repo_id,
                source_ref=source_ref,
                codebase_changes=codebase_changes,
                api_wrapper=repo_client,
            ),
            AppendToFileTool(
                source_repo_id=source_repo_id,
                source_ref=source_ref,
                codebase_changes=codebase_changes,
                api_wrapper=repo_client,
            ),
        ])
        return super_instance
