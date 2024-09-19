import logging
import textwrap

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from automation.graphs.codebase_search.agent import CodebaseSearchAgent
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex

logger = logging.getLogger(__name__)


class RepositoryTreeInput(BaseModel):
    """
    Use this as the primary tool to help find files or folders in the repository.
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
    Use this as the primary tool to get the content of a file from a repository.
    """

    file_path: str = Field(description="The file path to get.")


class CodebaseSearchInput(BaseModel):
    """
    Search for code snippets in the codebase.

    The query must be optimized for hybrid search: vectorstore retrieval and/or sparse retrieval.

    Example queries:
    - "Implementation of function foo"
    - "Implementation of class CharField"
    """

    query: str = Field(description=("The query to search for."))
    intent: str = Field(description=("The intent of the search query, why you are searching for this code."))


class GitLabTool(BaseTool):
    source_repo_id: str = Field(description="The repository ID to search in.")
    source_ref: str = Field(description="The branch or commit to search in.")

    api_wrapper: RepoClient = Field(default_factory=RepoClient.create_instance)


class RepositoryTreeTool(GitLabTool):
    name: str = "get_repository_tree"
    description: str = "Use this as the primary tool to help find files or folders in the repository."

    args_schema: type[BaseModel] = RepositoryTreeInput

    def _run(self, path: str, repo_id: str, ref: str) -> str:
        if tree := self.api_wrapper.get_repository_tree(repo_id, ref, path=path):
            return f"Repository files and directories found in {path}: {", ".join(tree)}"
        return f"No files/directories found in {path}."


class RepositoryFileTool(GitLabTool):
    name: str = "get_repository_file"
    description: str = "Use this as the primary tool to get the content of a file from a repository."

    args_schema: type[BaseModel] = RepositoryFileInput

    def _run(self, file_path: str) -> str:
        if repo_file := self.api_wrapper.get_repository_file(self.source_repo_id, file_path, self.source_ref):
            return textwrap.dedent(
                """\
                <RepositoryFile path="{file_path}">
                {repo_file}
                </RepositoryFile>
                """
            ).format(file_path=file_path, repo_file=repo_file)
        return f"error: File '{file_path}' not found."


class CodebaseSearchTool(BaseTool):
    name: str = "codebase_search"
    description: str = "Search for code snippets in the codebase."

    args_schema: type[BaseModel] = CodebaseSearchInput

    source_repo_id: str = Field(description="The repository ID to search in.")
    api_wrapper: CodebaseIndex = Field(default_factory=lambda: CodebaseIndex(repo_client=RepoClient.create_instance()))

    def _run(self, query: str, intent: str) -> str:
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
