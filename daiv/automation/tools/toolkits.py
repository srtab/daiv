from __future__ import annotations

from abc import ABCMeta, abstractmethod

from langchain_core.tools.base import BaseTool
from langchain_core.tools.base import BaseToolkit as LangBaseToolkit

from automation.tools.sandbox import RunSandboxCodeTool
from automation.tools.web_search import WebSearchTool

from .repository import (
    CreateNewRepositoryFileTool,
    DeleteRepositoryFileTool,
    RenameRepositoryFileTool,
    ReplaceSnippetInFileTool,
    RepositoryStructureTool,
    RetrieveFileContentTool,
    SearchCodeSnippetsTool,
)


class BaseToolkit(LangBaseToolkit, metaclass=ABCMeta):
    tools: list[BaseTool]

    @classmethod
    @abstractmethod
    def create_instance(cls) -> BaseToolkit:
        pass

    def get_tools(self) -> list[BaseTool]:
        return self.tools


class ReadRepositoryToolkit(BaseToolkit):
    """
    Toolkit for inspecting codebases.
    """

    @classmethod
    def create_instance(cls) -> BaseToolkit:
        return cls(tools=[SearchCodeSnippetsTool(), RetrieveFileContentTool(), RepositoryStructureTool()])


class WriteRepositoryToolkit(ReadRepositoryToolkit):
    """
    Toolkit for modifying codebases.
    """

    tools: list[BaseTool]

    @classmethod
    def create_instance(cls) -> BaseToolkit:
        super_instance = super().create_instance()
        super_instance.tools.extend([
            ReplaceSnippetInFileTool(),
            CreateNewRepositoryFileTool(),
            RenameRepositoryFileTool(),
            DeleteRepositoryFileTool(),
        ])
        return super_instance


class SandboxToolkit(BaseToolkit):
    """
    Toolkit for running code in a sandbox environment.
    """

    @classmethod
    def create_instance(cls) -> BaseToolkit:
        return cls(tools=[RunSandboxCodeTool()])


class WebSearchToolkit(BaseToolkit):
    """
    Toolkit for performing web searches.
    """

    @classmethod
    def create_instance(cls) -> BaseToolkit:
        return cls(tools=[WebSearchTool()])
