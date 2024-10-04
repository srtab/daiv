import logging
import textwrap

from langchain.callbacks.manager import CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from automation.graphs.codebase_search import CodebaseSearchAgent
from automation.utils import find_original_snippet
from codebase.base import CodebaseChanges, FileChange, FileChangeAction
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex

from .schemas import (
    AppendToFileInput,
    CreateFileInput,
    DeleteFileInput,
    RenameFileInput,
    ReplaceSnippetWithInput,
    RepositoryFileInput,
    RepositoryTreeInput,
    SearchRepositoryInput,
)

logger = logging.getLogger("daiv.tools")


class SearchRepositoryTool(BaseTool):
    name: str = "search_code_snippets"
    description: str = textwrap.dedent(
        """\
        Search for code snippets in the repository.

        Only use this tool if you don't know the exact file path to obtain from the repository.
        """
    )

    args_schema: type[BaseModel] = SearchRepositoryInput

    source_repo_id: str = Field(description="The repository ID to search in.")
    api_wrapper: CodebaseIndex = Field(default_factory=lambda: CodebaseIndex(repo_client=RepoClient.create_instance()))

    def _run(self, query: str, intent: str, run_manager: CallbackManagerForToolRun | None = None) -> str:
        """
        Searches the codebase for a given query.

        Args:
            query: The query to search for.
            intent: The intent of the search query, why you are searching for this code.

        Returns:
            The search results.
        """
        logger.debug("[codebase_search] Searching codebase for '%s'", query)

        search_results_str = "No search results found."

        search = CodebaseSearchAgent(source_repo_id=self.source_repo_id, index=self.api_wrapper)

        if search_results := search.agent.invoke({"query": query, "query_intent": intent}).get("documents"):
            search_results_str = ""
            for document in search_results:
                logger.debug("[codebase_search] Found snippet in '%s'", document.metadata["source"])

                search_results_str += textwrap.dedent(
                    """\
                    <CodeSnippet path="{file_path}">
                    {content}
                    </CodeSnippet>
                    """
                ).format(file_path=document.metadata["source"], content=document.page_content)

        return search_results_str


class BaseRepositoryTool(BaseTool):
    """
    Base class for repository interaction tools.
    """

    source_repo_id: str = Field(description="The repository ID to search in.")
    source_ref: str = Field(description="The branch or commit to search in.")

    codebase_changes: CodebaseChanges = Field(default_factory=CodebaseChanges)

    api_wrapper: RepoClient = Field(default_factory=RepoClient.create_instance)

    def _get_file_content(self, file_path: str) -> str | None:
        """
        Gets the content of a file to replace a snippet in.

        Args:
            file_path: The file path to get the content of.

        Returns:
            The content of the file.
        """
        if file_path not in self.codebase_changes.file_changes:
            return self.api_wrapper.get_repository_file(self.source_repo_id, file_path, self.source_ref)

        return self.codebase_changes.file_changes[file_path].content


class RepositoryTreeTool(BaseRepositoryTool):
    name: str = "get_repository_tree"
    description: str = textwrap.dedent(
        """\
        Tool to navigate through directories. Use it to find files or folders in the repository.
        Only use it if you don't know the exact file path to obtain from the repository.
        """
    )

    args_schema: type[BaseModel] = RepositoryTreeInput

    def _run(self, path: str, intent: str, run_manager: CallbackManagerForToolRun | None = None) -> str:
        """
        Gets the files and directories in a repository.

        Args:
            path: The path to search in.

        Returns:
            The files and directories in the repository.
        """
        logger.debug("[get_repository_tree] Getting files and directories in '%s' (intent: %s)", path, intent)
        if tree := self.api_wrapper.get_repository_tree(self.source_repo_id, self.source_ref, path=path):
            return f"Repository files and directories found in {path}: {", ".join(tree)}"
        return f"No files/directories found in {path}."


class RepositoryFileTool(BaseRepositoryTool):
    name: str = "get_repository_file"
    description: str = "Use this as the primary tool to get the content of a file from a repository."

    args_schema: type[BaseModel] = RepositoryFileInput

    def _run(self, file_path: str, intent: str, run_manager: CallbackManagerForToolRun | None = None) -> str:
        """
        Gets the content of a file from the repository.

        Args:
            file_path: The file path to get the content of.

        Returns:
            The content of the file.
        """
        logger.debug("[get_repository_file] Getting file '%s' (intent: %s)", file_path, intent)

        content = self._get_file_content(file_path)

        if not content:
            return f"error: File '{file_path}' not found."

        return textwrap.dedent(
            """\
            <RepositoryFile path="{file_path}">
            {content}
            </RepositoryFile>
            """
        ).format(file_path=file_path, content=content)


class ReplaceSnippetWithTool(BaseRepositoryTool):
    name: str = "replace_snippet_with"
    description: str = textwrap.dedent(
        """\
        Use this as the primary tool to write code changes to an existing file.

        Replaces a snippet in a file with the provided replacement.
        - The snippet must be an exact match;
        - The replacement can be any string;
        - The original snippet must be an entire line, not just a substring of a line. It should also include the indentation and spacing;
        - Indentation and spacing must be included in the replacement snippet;
        - If multiple replacements needed, call this function multiple times.
        """  # noqa: E501
    )

    args_schema: type[BaseModel] = ReplaceSnippetWithInput

    def _run(
        self,
        file_path: str,
        original_snippet: str,
        replacement_snippet: str,
        commit_message: str,
        run_manager: CallbackManagerForToolRun | None = None,
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
        logger.debug("[replace_snippet_with] Replacing snippet in file '%s'", file_path)

        if (
            file_path in self.codebase_changes.file_changes
            and self.codebase_changes.file_changes[file_path].action == FileChangeAction.DELETE
        ):
            return "error: You previously marked {file_path} to be deleted."

        if not (repo_file_content := self._get_file_content(file_path)):
            return f"error: File {file_path} not found."

        original_snippet_found = find_original_snippet(original_snippet, repo_file_content, initial_line_threshold=1)
        if not original_snippet_found:
            return "error: Original snippet not found."

        replaced_content = repo_file_content.replace(original_snippet_found, replacement_snippet)
        if not replaced_content:
            return "error: Snippet replacement failed."

        # Add a trailing snippet to the new snippet to match the original snippet if there isn't already one.
        if not replaced_content.endswith("\n"):
            replaced_content += "\n"

        if file_path in self.codebase_changes.file_changes:
            self.codebase_changes.file_changes[file_path].content = replaced_content
            self.codebase_changes.file_changes[file_path].commit_messages.append(commit_message)
        else:
            self.codebase_changes.file_changes[file_path] = FileChange(
                action=FileChangeAction.UPDATE,
                file_path=file_path,
                content=replaced_content,
                commit_messages=[commit_message],
            )

        return "success: Snippet replaced."


class CreateFileTool(BaseRepositoryTool):
    name: str = "create_file"
    description: str = textwrap.dedent(
        """\
        Use this as primary tool to create a new file with the provided content.
        Only use this to create inexistent files.
        """  # noqa: E501
    )

    args_schema: type[BaseModel] = CreateFileInput

    def _run(
        self, file_path: str, content: str, commit_message: str, run_manager: CallbackManagerForToolRun | None = None
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
        logger.debug("[create_file] Creating new file '%s'", file_path)

        if file_path in self.codebase_changes.file_changes or self.api_wrapper.repository_file_exists(
            self.source_repo_id, file_path, self.source_ref
        ):
            return "File already exists. Use 'replace_snippet_with' to update the file instead."

        self.codebase_changes.file_changes[file_path] = FileChange(
            action=FileChangeAction.CREATE, file_path=file_path, content=content, commit_messages=[commit_message]
        )

        return f"success: Created new file {file_path}."


class RenameFileTool(BaseRepositoryTool):
    name: str = "rename_file"
    description: str = "Use this as the primary tool to rename a file."

    args_schema: type[BaseModel] = RenameFileInput

    def _run(
        self,
        file_path: str,
        new_file_path: str,
        commit_message: str,
        run_manager: CallbackManagerForToolRun | None = None,
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
        logger.debug("[rename_file] Renaming file '%s' to '%s'", file_path, new_file_path)

        if new_file_path in self.codebase_changes.file_changes or self.api_wrapper.repository_file_exists(
            self.source_repo_id, new_file_path, self.source_ref
        ):
            return f"error: File {new_file_path} already exists."

        self.codebase_changes.file_changes[new_file_path] = FileChange(
            action=FileChangeAction.MOVE,
            file_path=new_file_path,
            previous_path=file_path,
            commit_messages=[commit_message],
        )

        return f"success: Renamed file {file_path} to {new_file_path}."


class DeleteFileTool(BaseRepositoryTool):
    name: str = "delete_file"
    description: str = "Use this as the primary tool to delete a file."

    args_schema: type[BaseModel] = DeleteFileInput

    def _run(self, file_path: str, commit_message: str, run_manager: CallbackManagerForToolRun | None = None) -> str:
        """
        Deletes a file in the repository.

        Args:
            file_path: The file path to delete.
            commit_message: The commit message to use for the deletion.

        Returns:
            A message indicating the success of the deletion.
        """
        logger.debug("[delete_file] Deleting file '%s'", file_path)

        if file_path in self.codebase_changes.file_changes:
            return f"error: File {file_path} has uncommited changes."

        if self.api_wrapper.repository_file_exists(self.source_repo_id, file_path, self.source_ref):
            self.codebase_changes.file_changes[file_path] = FileChange(
                action=FileChangeAction.DELETE, file_path=file_path, commit_messages=[commit_message]
            )
            return f"success: Deleted file {file_path}."

        return f"error: File {file_path} not found."


class AppendToFileTool(BaseRepositoryTool):
    name: str = "append_to_file"
    description: str = "Use this as the primary tool to append content to the end of a file."

    args_schema: type[BaseModel] = AppendToFileInput

    def _run(
        self, file_path: str, content: str, commit_message: str, run_manager: CallbackManagerForToolRun | None = None
    ) -> str:
        """
        Appends content to a file in the repository.

        Args:
            file_path: The file path to append to.
            content: The content to append.
            commit_message: The commit message to use for the appending.

        Returns:
            A message indicating the success of the appending.
        """
        logger.debug("[append_to_file] Appending content to file '%s'", file_path)

        # Add a trailing snippet to the new snippet to match the original snippet if there isn't already one.
        if not content.endswith("\n"):
            content += "\n"

        if file_path in self.codebase_changes.file_changes:
            self.codebase_changes.file_changes[file_path].content += content
            self.codebase_changes.file_changes[file_path].commit_messages.append(commit_message)
        elif repo_file_content := self._get_file_content(file_path):
            self.codebase_changes.file_changes[file_path] = FileChange(
                action=FileChangeAction.UPDATE,
                file_path=file_path,
                content=repo_file_content + content,
                commit_messages=[commit_message],
            )
        else:
            return f"error: File {file_path} not found."
        return f"success: Appended content to file {file_path}."
