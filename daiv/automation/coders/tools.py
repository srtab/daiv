import logging
import textwrap

from automation.agents.models import Usage
from automation.agents.tools import FunctionTool
from automation.coders.paths_replacer.coder import PathsReplacerCoder
from automation.coders.replacer import ReplacerCoder
from automation.coders.schemas import (
    CodebaseSearch,
    CreateFile,
    DeleteFile,
    GetRepositoryFile,
    GetRepositoryTree,
    RenameFile,
    ReplaceSnippetWith,
)
from codebase.base import FileChange, FileChangeAction
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex

logger = logging.getLogger(__name__)


class CodeInspectTools:
    """
    A class that provides tools for code inspection.
    """

    def __init__(
        self,
        repo_client: RepoClient,
        codebase_index: CodebaseIndex,
        usage: Usage,
        *,
        repo_id: str,
        ref: str | None = None,
    ):
        """
        Initializes the code inspection tools.

        Args:
            repo_client: The repository client to use.
            codebase_index: The codebase index to use.
            usage: The usage to use.
            repo_id: The repository ID to use.
            ref: The reference to use.
        """
        self.repo_client = repo_client
        self.codebase_index = codebase_index
        self.usage = usage
        self.repo_id = repo_id
        self.ref = ref

    def get_repository_tree(self, path: str = "") -> str:
        """
        Gets the repository tree.

        Args:
            path: The path to get the tree of.

        Returns:
            The repository tree.
        """
        logger.debug("[CodeInspectTools.get_repository_tree] Getting repository tree %s", path)

        if tree := self.repo_client.get_repository_tree(self.repo_id, self.ref, path=path):
            return f"Repository files and directories found in {path}: {", ".join(tree)}"
        return f"No files/directories found in {path}."

    def get_repository_file(self, file_path: str) -> str:
        """
        Gets the content of a repository file.

        Args:
            file_path: The file path to get the content of.

        Returns:
            The content of the file. If the file is not found, returns an error message.
        """
        logger.debug("[CodeInspectTools.get_repository_file] Getting repository file %s", file_path)
        if repo_file := self.repo_client.get_repository_file(self.repo_id, file_path, self.ref):
            return textwrap.dedent(
                """\
                file path: {file_path}
                ```
                {repo_file}
                ```
                """
            ).format(file_path=file_path, repo_file=repo_file)
        return f"error: File '{file_path}' not found."

    def codebase_search(self, query: str) -> str:
        """
        Search for code snippets in the codebase

        Args:
            query: The query to search for.

        Returns:
            The search results.
        """
        logger.debug("[CodeInspectTools.codebase_search] Searching codebase for %s", query)

        search_results_str = "No search results found."

        if search_results := self.codebase_index.search_with_reranker(self.repo_id, query, k=5):
            search_results_str = "### Search results ###"
            for reranked_score, result in search_results:
                logger.debug(
                    "[CodeInspectTools.codebase_search] file_path=%s score=%r",
                    result.document.metadata["source"],
                    reranked_score,
                )
                search_results_str += textwrap.dedent(
                    """\
                    \n\n
                    file path: {file_path}
                    ```{language}
                    {content}
                    ```
                    """
                ).format(
                    file_path=result.document.metadata["source"],
                    language=result.document.metadata.get("language", ""),
                    content=result.document.page_content,
                )

        return search_results_str

    def get_tools(self):
        """
        Gets the tools for the code inspection.
        """
        return [
            FunctionTool(schema_model=GetRepositoryFile, fn=self.get_repository_file),
            FunctionTool(schema_model=GetRepositoryTree, fn=self.get_repository_tree),
            FunctionTool(schema_model=CodebaseSearch, fn=self.codebase_search),
        ]


class CodeActionTools(CodeInspectTools):
    """
    A class that provides tools for code actions.
    """

    def __init__(self, *args, replace_paths: bool = False, **kwargs):
        """
        Initializes the code action tools.

        Args:
            replace_paths: Whether to replace paths in the snippets. Usefull for cross projects coding.
        """
        super().__init__(*args, **kwargs)
        self.replace_paths = replace_paths
        self.file_changes: dict[str, FileChange] = {}

    def replace_snippet_with(
        self, file_path: str, original_snippet: str, replacement_snippet: str, commit_message: str
    ):
        """
        Replaces a snippet with the provided replacement.

        Args:
            file_path: The file path to replace the snippet in.
            original_snippet: The original snippet to replace.
            replacement_snippet: The replacement snippet.
            commit_message: The commit message to use for the replacement.

        Returns:
            A message indicating the success of the replacement.
        """
        logger.debug(
            "[CodeActionTools.replace_snippet_with] Replacing snippet\n```\n%s\n```\n with \n```\n%s\n```\nin %s",
            original_snippet,
            replacement_snippet,
            file_path,
        )

        if file_path in self.file_changes and self.file_changes[file_path].action == FileChangeAction.DELETE:
            raise Exception("File is marked to be deleted.")

        repo_file_content = self._get_file_content(file_path)

        if self.replace_paths:
            replacement_snippet = self._replace_paths(replacement_snippet)

        replaced_content = ReplacerCoder(self.usage).invoke(
            original_snippet=original_snippet, replacement_snippet=replacement_snippet, content=repo_file_content
        )

        if not replaced_content:
            raise Exception("Snippet replacement failed.")

        # Add a trailing snippet to the new snippet to match the original snippet if there isn't already one.
        if not replaced_content.endswith("\n"):
            replaced_content += "\n"

        if file_path in self.file_changes:
            self.file_changes[file_path].content = replaced_content
            self.file_changes[file_path].commit_messages.append(commit_message)
        else:
            self.file_changes[file_path] = FileChange(
                action=FileChangeAction.UPDATE,
                file_path=file_path,
                content=replaced_content,
                commit_messages=[commit_message],
            )

        return "success: Snippet replaced."

    def _get_file_content(self, file_path: str) -> str | None:
        """
        Gets the content of a file to replace a snippet in.

        Args:
            file_path: The file path to get the content of.

        Returns:
            The content of the file.
        """
        if file_path not in self.file_changes:
            return self.repo_client.get_repository_file(self.repo_id, file_path, self.ref)

        return self.file_changes[file_path].content

    def create_file(self, file_path: str, content: str, commit_message: str):
        """
        Creates a new file with the provided content in the repository.

        Args:
            file_path: The file path to create.
            content: The content of the file.
            commit_message: The commit message to use for the creation.

        Returns:
            A message indicating the success of the creation
        """
        logger.debug("[CodeActionTools.create_file] Creating new file %s", file_path)

        if file_path in self.file_changes:
            raise Exception("File already exists.")

        if self.replace_paths:
            content = self._replace_paths(content)

        self.file_changes[file_path] = FileChange(
            action=FileChangeAction.CREATE, file_path=file_path, content=content, commit_messages=[commit_message]
        )

        return f"success: Created new file {file_path}."

    def rename_file(self, file_path: str, new_file_path: str, commit_message: str):
        """
        Renames a file on the repository.

        Args:
            file_path: The file path to rename.
            new_file_path: The new file path.
            commit_message: The commit message to use for the rename.

        Returns:
            A message indicating the success of the rename.
        """
        logger.debug("[CodeActionTools.rename_file] Renaming file %s to %s", file_path, new_file_path)

        if new_file_path in self.file_changes:
            raise Exception("New file already exists.")

        self.file_changes[new_file_path] = FileChange(
            action=FileChangeAction.MOVE,
            file_path=new_file_path,
            previous_path=file_path,
            commit_messages=[commit_message],
        )

        return f"success: Renamed file {file_path} to {new_file_path}."

    def delete_file(self, file_path: str, commit_message: str):
        """
        Deletes a file from the repository.

        Args:
            file_path: The file path to delete.
            commit_message: The commit message to use for the deletion.

        Returns:
            A message indicating the success of the deletion.
        """
        logger.debug("[CodeActionTools.delete_file] Deleting file %s", file_path)

        self.file_changes[file_path] = FileChange(
            action=FileChangeAction.DELETE, file_path=file_path, commit_messages=[commit_message]
        )

        return f"success: Deleted file {file_path}."

    def _replace_paths(self, replacement_snippet: str) -> str:
        """
        Replaces paths in the replacement snippet.

        Args:
            replacement_snippet: The snippet to replace paths in.

        Returns:
            The snippet with paths replaced.
        """
        # TODO: optimize to avoid calling this too many times, the repository tree should be cached in some way.
        repository_tree = self.codebase_index.extract_tree(self.repo_id, self.ref)

        replacement_snippet_result = PathsReplacerCoder(self.usage).invoke(
            code_snippet=replacement_snippet, repository_tree=repository_tree
        )

        if replacement_snippet_result is None:
            logger.warning("No paths replaced from the replacement snippet.")
            return replacement_snippet

        return replacement_snippet_result

    def get_tools(self):
        """
        Gets the tools for the code actions.
        """
        return super().get_tools() + [
            FunctionTool(schema_model=ReplaceSnippetWith, fn=self.replace_snippet_with),
            FunctionTool(schema_model=CreateFile, fn=self.create_file),
            FunctionTool(schema_model=RenameFile, fn=self.rename_file),
            FunctionTool(schema_model=DeleteFile, fn=self.delete_file),
        ]
