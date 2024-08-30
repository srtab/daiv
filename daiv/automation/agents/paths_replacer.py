import textwrap

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

from codebase.clients import RepoClient


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


class GitLabTool(BaseTool):
    repo_id: str
    """The repository ID."""
    ref: str
    """The branch or commit reference."""

    api_wrapper: RepoClient = Field(default_factory=RepoClient.create_instance)


class RepositoryTreeTool(GitLabTool):
    name: str = "get_repository_tree"
    description: str = "Use this as the primary tool to help find files or folders in the repository."
    args_schema: type[BaseModel] = RepositoryTreeInput

    def _run(self, path: str, run_manager: CallbackManagerForToolRun | None = None) -> str:
        if tree := self.api_wrapper.get_repository_tree(self.repo_id, self.ref, path=path):
            return f"Repository files and directories found in {path}: {", ".join(tree)}"
        return f"No files/directories found in {path}."


class RepositoryFileTool(GitLabTool):
    name = "get_repository_file"
    description = "Use this as the primary tool to get the content of a file from a repository."
    args_schema: type[BaseModel] = RepositoryFileInput

    def _run(self, file_path: str, run_manager: CallbackManagerForToolRun | None = None) -> str:
        if repo_file := self.api_wrapper.get_repository_file(self.repo_id, file_path, self.ref):
            return textwrap.dedent(
                """\
                file path: {file_path}
                ```
                {repo_file}
                ```
                """
            ).format(file_path=file_path, repo_file=repo_file)
        return f"error: File '{file_path}' not found."


class ExtractedPaths(BaseModel):
    paths: list[str] = Field(description="Valid filesystem paths found in the code snippet.")


def extract_paths():
    prompt_template = ChatPromptTemplate.from_messages([
        (
            "system",
            textwrap.dedent(
                """\
                Act as an exceptional senior software engineer that is specialized in extraction algorithm.
                It's absolutely vital that you completely and correctly execute your task.
                """
            ),
        ),
        (
            "user",
            textwrap.dedent(
                """\
                ### Task ###
                Search for valid filesystem paths on the code snippet below.
                Identify clearly which paths belong to a project and only considers those.
                Ignore external paths, don't include them on the output.
                If you find a path with a variable, ignore it.

                ### Code Snippet ###
                {code_snippet}
                """
            ),
        ),
    ])

    model = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    chain = prompt_template | model.with_structured_output(ExtractedPaths)

    return chain.invoke({"changes": "- Added a new feature\n- Fixed a bug"})


tool_node = ToolNode([])
