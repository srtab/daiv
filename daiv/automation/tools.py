import logging
import textwrap

from langchain.callbacks.manager import CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from automation.graphs.codebase_search.agent import CodebaseSearchAgent
from automation.utils import find_original_snippet
from codebase.base import CodebaseChanges, FileChange, FileChangeAction
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex

logger = logging.getLogger(__name__)


class RepositoryTreeInput(BaseModel):
    """
    Obtain the tree of the repository from a given path.
    """

    path: str = Field(
        description=(
            "The path inside the repository. "
            "The default is the root of the repository. "
            "Use it to navigate through directories."
        ),
        default="",
    )


class RepositoryFileInput(BaseModel):
    """
    Get the content of a file from the repository.
    """

    file_path: str = Field(description="The file path to get.")


class CodebaseSearchInput(BaseModel):
    """
    Search for code snippets in the codebase.
    """

    query: str = Field(description=("The query to search for."))
    intent: str = Field(description=("The intent of the search query, why you are searching for this code."))


class ReplaceSnippetWithInput(BaseModel):
    """
    Replaces a snippet in a file with the provided replacement.
    """  # noqa: E501

    file_path: str = Field(description="The file_path of code to refactor. Ignore referenced unified diff file path.")
    original_snippet: str = Field(description="The snippet to replace.")
    replacement_snippet: str = Field(description="The replacement for the snippet.")
    commit_message: str = Field(description="The commit message to use.")


class GitLabTool(BaseTool):
    source_repo_id: str = Field(description="The repository ID to search in.")
    source_ref: str = Field(description="The branch or commit to search in.")

    codebase_changes: CodebaseChanges

    api_wrapper: RepoClient = Field(default_factory=RepoClient.create_instance)


class RepositoryTreeTool(GitLabTool):
    name: str = "get_repository_tree"
    description: str = "Use this as the primary tool to help find files or folders in the repository."

    args_schema: type[BaseModel] = RepositoryTreeInput

    def _run(self, path: str, repo_id: str, ref: str, run_manager: CallbackManagerForToolRun | None = None) -> str:
        if tree := self.api_wrapper.get_repository_tree(repo_id, ref, path=path):
            return f"Repository files and directories found in {path}: {", ".join(tree)}"
        return f"No files/directories found in {path}."


class RepositoryFileTool(GitLabTool):
    name: str = "get_repository_file"
    description: str = "Use this as the primary tool to get the content of a file from a repository."

    args_schema: type[BaseModel] = RepositoryFileInput

    def _run(self, file_path: str, run_manager: CallbackManagerForToolRun | None = None) -> str:
        content: str | None = None

        if file_path in self.codebase_changes.file_changes:
            content = self.codebase_changes.file_changes[file_path].content
        elif repo_file := self.api_wrapper.get_repository_file(self.source_repo_id, file_path, self.source_ref):
            content = repo_file

        if not content:
            return f"error: File '{file_path}' not found."

        return textwrap.dedent(
            """\
            <RepositoryFile path="{file_path}">
            {content}
            </RepositoryFile>
            """
        ).format(file_path=file_path, content=content)


class CodebaseSearchTool(BaseTool):
    name: str = "codebase_search"
    description: str = textwrap.dedent(
        """\
        Search for code snippets in the codebase.

        Only use this tool if you don't know the exact file path to obtain from the repository.

        The query must be optimized for hybrid search: vectorstore retrieval and/or sparse retrieval.

        Example queries:
        - "Implementation of function foo"
        - "Implementation of class CharField"
        """
    )

    args_schema: type[BaseModel] = CodebaseSearchInput

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
        logger.debug("[CodeInspectTools.codebase_search] Searching codebase for %s", query)

        search_results_str = "No search results found."

        agent = CodebaseSearchAgent(source_repo_id=self.source_repo_id, index=self.api_wrapper)

        if search_results := agent.graph.invoke({"query": query, "query_intent": intent}).get("documents"):
            search_results_str = "### Search results ###"
            for document in search_results:
                logger.debug("[CodeInspectTools.codebase_search] file_path=%s", document.metadata["source"])

                search_results_str += textwrap.dedent(
                    """\
                    <CodeSnippet path="{file_path}" language="{language}">
                    {content}
                    </CodeSnippet>
                    """
                ).format(
                    file_path=document.metadata["source"],
                    language=document.metadata.get("language", ""),
                    content=document.page_content,
                )

        return search_results_str


class ReplaceSnippetWithTool(GitLabTool):
    name: str = "replace_snippet_with"
    description: str = textwrap.dedent(
        """\
        Use this as the primary tool to write code changes to a file.

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
        if (
            file_path in self.codebase_changes.file_changes
            and self.codebase_changes.file_changes[file_path].action == FileChangeAction.DELETE
        ):
            raise Exception("File is marked to be deleted.")

        repo_file_content = self._get_file_content(file_path)
        if not repo_file_content:
            raise ValueError(f"File {file_path} not found.")

        original_snippet_found = find_original_snippet(
            original_snippet, repo_file_content, threshold=0.75, initial_line_threshold=0.95
        )
        if not original_snippet_found:
            raise Exception("Original snippet not found.")

        replaced_content = repo_file_content.replace(original_snippet_found, replacement_snippet)

        if not replaced_content:
            raise Exception("Snippet replacement failed.")

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
