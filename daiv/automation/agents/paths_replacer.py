import textwrap
from typing import Annotated, Literal

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph
from langgraph.graph.message import add_messages
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


def extract_paths_agent(code_snippet: str) -> ExtractedPaths:
    prompt_template = ChatPromptTemplate.from_messages([
        SystemMessage(
            textwrap.dedent(
                """\
                Act as an exceptional senior software engineer that is specialized in extraction algorithm.
                It's absolutely vital that you completely and correctly execute your task.
                """
            )
        ),
        HumanMessage(
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
            )
        ),
    ])

    model = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    chain = prompt_template | model.with_structured_output(ExtractedPaths, strict=True)

    return chain.invoke({"code_snippet": code_snippet})


tools = [RepositoryTreeTool()]


class PathsReplacerState(BaseModel):
    code_snippet: str
    extracted_paths: ExtractedPaths
    path_replacements: list[str]
    messages: Annotated[list, add_messages]


def should_continue(state: PathsReplacerState) -> Literal["tools", "__end__"]:
    messages = state["extracted_paths"]
    last_message = messages[-1]
    if last_message.tool_calls:
        return "tools"
    return "__end__"


workflow = StateGraph(PathsReplacerState)


@workflow.add_node
def call_extractor(state: PathsReplacerState):
    return {"extracted_paths": extract_paths_agent(state.code_snippet)}


@workflow.add_node
def call_replacer(state: PathsReplacerState):
    return {"path_replacements": extract_paths_agent(state.code_snippet)}


# Define the two nodes we will cycle between
workflow.add_node("tools", ToolNode(tools))

workflow.add_edge("__start__", "paths_extractor")
workflow.add_edge("paths_extractor", "paths_replacer")
workflow.add_edge("tools", "paths_replacer")

workflow.add_conditional_edges("paths_extractor", should_continue)

workflow.add_conditional_edges("paths_replacer", should_continue)

app = workflow.compile()
