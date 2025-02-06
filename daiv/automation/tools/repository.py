from __future__ import annotations

import logging
import textwrap

from langchain_core.runnables import RunnableConfig  # noqa: TC002
from langchain_core.tools import BaseTool
from langgraph.store.memory import BaseStore  # noqa: TC002
from pydantic import BaseModel, Field

from automation.agents.codebase_search import CodebaseSearchAgent
from automation.agents.snippet_replacer.agent import SnippetReplacerAgent
from automation.utils import file_changes_namespace
from codebase.base import ClientType, FileChange, FileChangeAction
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex
from core.utils import build_uri

from .schemas import (
    CreateNewRepositoryFileInput,
    DeleteRepositoryFileInput,
    RenameRepositoryFileInput,
    ReplaceSnippetInFileInput,
    RetrieveFileContentInput,
    SearchCodeSnippetsInput,
)

logger = logging.getLogger("daiv.tools")

EXPLORE_REPOSITORY_PATH_NAME = "explore_repository_path"
RETRIEVE_FILE_CONTENT_NAME = "retrieve_file_content"
SEARCH_CODE_SNIPPETS_NAME = "search_code_snippets"
REPLACE_SNIPPET_IN_FILE_NAME = "replace_snippet_in_file"
CREATE_NEW_REPOSITORY_FILE_NAME = "create_new_repository_file"
RENAME_REPOSITORY_FILE_NAME = "rename_repository_file"
DELETE_REPOSITORY_FILE_NAME = "delete_repository_file"
APPEND_TO_REPOSITORY_FILE_NAME = "append_to_repository_file"


class SearchCodeSnippetsTool(BaseTool):
    name: str = SEARCH_CODE_SNIPPETS_NAME
    description: str = textwrap.dedent(
        """\
        Search for code snippets in the repository based on a code-focused query.
        Use when you do not know the exact file path. The returned value will include only partial pieces of code.
        If you know the exact file path or need full content of the file, use '{retrieve_file_content_name}' instead.
        """  # noqa: E501
    ).format(retrieve_file_content_name=RETRIEVE_FILE_CONTENT_NAME)
    args_schema: type[BaseModel] = SearchCodeSnippetsInput
    handle_validation_error: bool = True

    api_wrapper: CodebaseIndex = Field(default_factory=lambda: CodebaseIndex(repo_client=RepoClient.create_instance()))

    def _run(self, query: str, intent: str, config: RunnableConfig) -> str:
        """
        Searches the codebase for a given query.

        Args:
            query: The query to search for.
            intent: The intent of the search query, why you are searching for this code.

        Returns:
            The search results.
        """
        logger.debug("[%s] Searching for '%s' (intent: %s)", self.name, query, intent)

        source_repo_id = config["configurable"].get("source_repo_id")
        source_ref = config["configurable"].get("source_ref")

        search_results_str = (
            "The query your provided did not return any results. "
            "This means that the code/definition/paths you are looking for is not present/defined in the codebase."
        )

        if source_repo_id and source_ref:
            # we need to update the index before retrieving the documents
            # because the codebase search agent needs to search for the codebase changes
            # and we need to make sure the index is updated before the agent starts retrieving the documents
            self.api_wrapper.update(source_repo_id, source_ref)

        search = CodebaseSearchAgent(
            retriever=self.api_wrapper.as_retriever(source_repo_id, source_ref), rephrase=False
        )

        if search_results := search.agent.invoke(query):
            search_results_str = ""
            for document in search_results:
                logger.debug("[%s] Found snippet in '%s'", self.name, document.metadata["source"])

                search_results_str += textwrap.dedent(
                    """\
                    <CodeSnippet repository="{repository_id}" ref="{ref}" path="{file_path}" external_link="{link}">
                    {content}
                    </CodeSnippet>
                    """
                ).format(
                    repository_id=document.metadata["repo_id"],
                    ref=document.metadata["ref"],
                    file_path=document.metadata["source"],
                    link=self._get_file_link(
                        document.metadata["repo_id"], document.metadata["ref"], document.metadata["source"]
                    ),
                    content=document.page_content,
                )

        return search_results_str

    def _get_file_link(self, repository_id: str, ref: str, file_path: str) -> str:
        if self.api_wrapper.repo_client.client_slug == ClientType.GITLAB:
            return build_uri(self.api_wrapper.repo_client.codebase_url, f"/{repository_id}/-/blob/{ref}/{file_path}")

        raise ValueError(f"Unsupported repository client type: {self.api_wrapper.repo_client.client_slug}")


class BaseRepositoryTool(BaseTool):
    """
    Base class for repository interaction tools.
    """

    handle_validation_error: bool = True

    api_wrapper: RepoClient = Field(default_factory=RepoClient.create_instance)

    def _get_file_content(self, file_path: str, store: BaseStore, source_repo_id: str, source_ref: str) -> str | None:
        """
        Gets the content of a file to replace a snippet in.

        Args:
            file_path: The file path to get the content of.

        Returns:
            The content of the file.
        """

        if stored_item := store.get(file_changes_namespace(source_repo_id, source_ref), file_path):
            return stored_item.value["data"].content

        return self.api_wrapper.get_repository_file(source_repo_id, file_path, source_ref)


class RetrieveFileContentTool(BaseRepositoryTool):
    name: str = RETRIEVE_FILE_CONTENT_NAME
    description: str = textwrap.dedent(
        """\
        Retrieve the content of a specified file path from a repository. Use this tool to get the full content of a file, and not only a snippet.
        The returned value will include full implementation, including used/declared imports.
        """  # noqa: E501
    )

    return_not_found_message: bool = Field(
        default=True, description="Whether to return a message if the file is not found. Otherwise, return None."
    )

    args_schema: type[BaseModel] = RetrieveFileContentInput

    def _run(self, file_path: str, intent: str, store: BaseStore, config: RunnableConfig) -> str | None:
        """
        Gets the content of a file from the repository.

        Args:
            file_path: The file path to get the content of.

        Returns:
            The content of the file.
        """
        logger.debug("[%s] Getting file '%s' (intent: %s)", self.name, file_path, intent)

        source_repo_id = config["configurable"]["source_repo_id"]
        source_ref = config["configurable"]["source_ref"]

        content = self._get_file_content(file_path, store, source_repo_id, source_ref)

        if not content:
            return f"error: File '{file_path}' not found." if self.return_not_found_message else None

        return textwrap.dedent(
            """\
            <repository_file path="{file_path}">
            {content}
            </repository_file>
            """
        ).format(file_path=file_path, content=content)


class ReplaceSnippetInFileTool(BaseRepositoryTool):
    name: str = REPLACE_SNIPPET_IN_FILE_NAME
    description: str = textwrap.dedent(
        """\
        Replace an exact matching snippet in a file with the provided replacement string. It should be used when you need to replace a specific code snippet in a file.
        For multiple replacements, call this tool multiple times.
        Do not alter indentation levels unless intentionally modifying code block structures.
        Inspect the code beforehand to understand what exaclty needs to change.

        IMPORTANT:
        - Provide at least 3 lines before and 3 lines after the snippet you want to replace.
        - Include unique identifiers such as variable names or function calls that appear only once in the entire file.
        """  # noqa: E501
    )

    args_schema: type[BaseModel] = ReplaceSnippetInFileInput

    def _run(
        self,
        file_path: str,
        original_snippet: str,
        replacement_snippet: str,
        commit_message: str,
        store: BaseStore,
        config: RunnableConfig,
    ) -> str:
        """
        Replaces a snippet in a file with the provided replacement.

        Args:
            file_path: The file path to replace the snippet in.
            original_snippet: The original snippet to replace.
            replacement_snippet: The replacement snippet.
            commit_message: The commit message to use for the replacement.

        Returns:
            A message indicating the success of the replacement.
        """
        logger.debug("[%s] Replacing snippet in file '%s'", self.name, file_path)

        source_repo_id = config["configurable"]["source_repo_id"]
        source_ref = config["configurable"]["source_ref"]

        stored_item = store.get(file_changes_namespace(source_repo_id, source_ref), file_path)

        file_change: FileChange | None = stored_item.value["data"] if stored_item else None

        if file_change and file_change.action == FileChangeAction.DELETE:
            return "error: You previously marked {file_path} to be deleted."

        if not (repo_file_content := self._get_file_content(file_path, store, source_repo_id, source_ref)):
            return f"error: File {file_path} not found."

        if original_snippet == replacement_snippet:
            return (
                "error: The original snippet and the replacement snippet are the same. "
                "No changes will be made. Make sure you're not missing any changes."
            )

        replacer = SnippetReplacerAgent()
        result = replacer.agent.invoke({
            "original_snippet": original_snippet,
            "replacement_snippet": replacement_snippet,
            "content": repo_file_content,
        })

        if isinstance(result, str):
            # It means, and error occurred during the replacement.
            return result

        if file_change:
            file_change.content = result.content
            file_change.commit_messages.append(commit_message)
        else:
            file_change = FileChange(
                action=FileChangeAction.UPDATE,
                file_path=file_path,
                content=result.content,
                commit_messages=[commit_message],
            )

        store.put(
            file_changes_namespace(source_repo_id, source_ref),
            file_path,
            {"data": file_change, "action": file_change.action},
        )

        return "success: Snippet replaced."


class CreateNewRepositoryFileTool(BaseRepositoryTool):
    name: str = CREATE_NEW_REPOSITORY_FILE_NAME
    description: str = textwrap.dedent(
        """\
        Create a new file within the repository with the provided file content. Use this tool only to create files that do not already exist in the repository. Do not use this tool to overwrite or modify existing files. Ensure that the file path does not point to an existing file in the repository. Necessary directories should already exist in the repository; this tool does not create directories.
        """  # noqa: E501
    )

    args_schema: type[BaseModel] = CreateNewRepositoryFileInput

    def _run(
        self, file_path: str, file_content: str, commit_message: str, store: BaseStore, config: RunnableConfig
    ) -> str:
        """
        Creates a new file with the provided content in the repository.

        Args:
            file_path: The file path to create.
            content: The content of the file.
            commit_message: The commit message to use for the creation.

        Returns:
            A message indicating the success of the creation
        """
        logger.debug("[%s] Creating new file '%s'", self.name, file_path)

        source_repo_id = config["configurable"]["source_repo_id"]
        source_ref = config["configurable"]["source_ref"]

        stored_item = store.get(file_changes_namespace(source_repo_id, source_ref), file_path)

        if stored_item or self.api_wrapper.repository_file_exists(source_repo_id, file_path, source_ref):
            return f"File already exists. Use '{REPLACE_SNIPPET_IN_FILE_NAME}' to update the file instead."

        store.put(
            file_changes_namespace(source_repo_id, source_ref),
            file_path,
            {
                "data": FileChange(
                    action=FileChangeAction.CREATE,
                    file_path=file_path,
                    content=file_content,
                    commit_messages=[commit_message],
                ),
                "action": FileChangeAction.CREATE,
            },
        )

        return f"success: Created new file {file_path}."


class RenameRepositoryFileTool(BaseRepositoryTool):
    name: str = RENAME_REPOSITORY_FILE_NAME
    description: str = textwrap.dedent(
        """\
        Rename an existing file within the repository to a new specified path. Do not use this tool to create new files or delete existing ones. Ensure that 'file_path' points to an existing file in the repository. Ensure that 'new_file_path' does not point to an existing file to prevent overwriting. Necessary directories for 'new_file_path' should already exist in the repository; this tool does not create directories.
        """  # noqa: E501
    )

    args_schema: type[BaseModel] = RenameRepositoryFileInput

    def _run(
        self, file_path: str, new_file_path: str, commit_message: str, store: BaseStore, config: RunnableConfig
    ) -> str:
        """
        Renames a file in the repository.

        Args:
            file_path: The file path to rename.
            new_file_path: The new file path.
            commit_message: The commit message to use for the renaming.

        Returns:
            A message indicating the success of the renaming.
        """
        logger.debug("[%s] Renaming file '%s' to '%s'", self.name, file_path, new_file_path)

        source_repo_id = config["configurable"]["source_repo_id"]
        source_ref = config["configurable"]["source_ref"]

        stored_item = store.get(file_changes_namespace(source_repo_id, source_ref), file_path)

        if stored_item or self.api_wrapper.repository_file_exists(source_repo_id, new_file_path, source_ref):
            return f"error: File {new_file_path} already exists."

        store.put(
            file_changes_namespace(source_repo_id, source_ref),
            new_file_path,
            {
                "data": FileChange(
                    action=FileChangeAction.MOVE,
                    file_path=new_file_path,
                    previous_path=file_path,
                    commit_messages=[commit_message],
                ),
                "action": FileChangeAction.MOVE,
            },
        )

        return f"success: Renamed file {file_path} to {new_file_path}."


class DeleteRepositoryFileTool(BaseRepositoryTool):
    name: str = DELETE_REPOSITORY_FILE_NAME
    description: str = textwrap.dedent(
        """\
        Delete an existing file from the repository. Use this tool to delete files that are no longer needed. Do not use this tool to delete directories or non-file entities. Ensure that 'file_path' points to an existing file in the repository. Exercise caution to avoid unintended data loss.
        """  # noqa: E501
    )

    args_schema: type[BaseModel] = DeleteRepositoryFileInput

    def _run(self, file_path: str, commit_message: str, store: BaseStore, config: RunnableConfig) -> str:
        """
        Deletes a file in the repository.

        Args:
            file_path: The file path to delete.
            commit_message: The commit message to use for the deletion.

        Returns:
            A message indicating the success of the deletion.
        """
        logger.debug("[%s] Deleting file '%s'", self.name, file_path)

        source_repo_id = config["configurable"]["source_repo_id"]
        source_ref = config["configurable"]["source_ref"]

        stored_item = store.get(file_changes_namespace(source_repo_id, source_ref), file_path)

        if stored_item:
            return f"error: File {file_path} has uncommited changes."

        if self.api_wrapper.repository_file_exists(source_repo_id, file_path, source_ref):
            store.put(
                file_changes_namespace(source_repo_id, source_ref),
                file_path,
                {
                    "data": FileChange(
                        action=FileChangeAction.DELETE, file_path=file_path, commit_messages=[commit_message]
                    ),
                    "action": FileChangeAction.DELETE,
                },
            )
            return f"success: Deleted file {file_path}."

        return f"error: File {file_path} not found."
