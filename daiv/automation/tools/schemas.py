import textwrap
from typing import Annotated, Any

from langgraph.prebuilt.tool_node import InjectedStore
from pydantic import Base64Bytes, BaseModel, Field


class SearchCodeSnippetsInput(BaseModel):
    """
    Search for code snippets in the codebase.
    """

    query: str = Field(
        ...,
        description=textwrap.dedent(
            """\
            A concise, code-centric string (≤150 chars) composed of **2-6 high-signal tokens** separated by spaces or new lines (bag-of-terms). The engine is case-insensitive and treats input as **literal tokens**—it does **not** interpret operators, wildcards, or regex; `AND`/`OR`/quotes/`-` are just characters. Multi-line is allowed within the limit.

            If the user asked in natural language, **rewrite** it into tokens here. Prefer:
            • Exact identifiers and API calls (e.g., `refreshAccessToken`, `jwt.verify(`, `useSWR(`)
            • Import/module strings and config keys (e.g., `import Stripe`, `NEXTAUTH_SECRET`)
            • Distinctive literals and error codes (e.g., `ERR_JWT_EXPIRED`, `\"Invalid signature\"`)
            Avoid: a single generic word, commas/quotes unless they are part of the code, filler like "code/snippet/example/sample", and repository names.

            **Multi-term examples:**
            • `constructEvent stripe webhook.ts`
            • `jwt.verify( refreshToken src/auth/`
            • `oauth callback github nextauth`
            • `Invalid signature payments webhook`"""  # noqa: E501
        ),
        max_length=150,
    )
    intent: str = Field(
        ...,
        description=(
            "Why you need this code, in one short sentence, to aid re-ranking. "
            "Include the user's task and desired follow-up action."
        ),
        examples=[
            "Count the agents defined in DAIV and list their names.",
            "Find where AgentRegistry.register is called to trace registration flow.",
            "Locate the enum that enumerates DAIV agents for documentation.",
        ],
    )


class CrossSearchCodeSnippetsInput(SearchCodeSnippetsInput):
    """
    Search for code snippets in specific repositories or all repositories.
    """

    repository: str | None = Field(
        default=None,
        description=(
            "Optional repository name to restrict the search. If null, searches across all available repositories. "
            "Use the canonical repo name; omit paths."
        ),
    )


class RepositoryStructureInput(BaseModel):
    """
    Get the structure of the repository.
    """

    intent: str = Field(
        description=(
            "Briefly state why you need the repository structure "
            "(e.g., 'locate auth middlewares and config files to scope logging changes')."
        )
    )


class CrossRepositoryStructureInput(RepositoryStructureInput):
    """
    Get the structure of the repository.
    """

    repository: str = Field(description="The name of the repository to get the structure of.")


class RetrieveFileContentInput(BaseModel):
    """
    Get the content of a file from the repository.
    """

    file_paths: list[str] = Field(
        description=(
            "The paths to the files to retrieve (e.g., 'example/tests/test_admin.py'). "
            "You can provide multiple file paths to retrieve the content of multiple files at once. "
        )
    )
    intent: str = Field(description="A description of why you're getting these files.")
    store: Annotated[Any, InjectedStore()]


class CrossRetrieveFileContentInput(RetrieveFileContentInput):
    """
    Get the content of a file from the repository.
    """

    repository: str = Field(description="The name of the repository to get the content of.")


class CommitableBaseModel(BaseModel):
    commit_message: str = Field(
        ...,
        description=(
            "The commit message to use. This will be used to describe the changes applied. "
            "Tip: Use action-oriented verbs, such as 'Added', 'Updated', 'Removed', 'Improved', etc..."
        ),
    )


class ReplaceSnippetInFileInput(CommitableBaseModel):
    """
    Replaces a snippet in a file with the provided replacement.
    """

    file_path: str = Field(..., description="The path to the file where the replacement will take place.")
    original_snippet: str = Field(
        ...,
        description=(
            "The exact sequence of line to be replaced, including **all indentation and spacing**.\n"
            "Tip: Copy the snippet directly from the file to ensure an exact match."
        ),
    )
    replacement_snippet: str = Field(
        ...,
        description=(
            "The new sequence of lines to replace the original, including the necessary indentation and "
            "spacing to fit seamlessly into the code.\n"
            "Tip: Align the indentation level with the surrounding code for consistency."
        ),
    )
    store: Annotated[Any, InjectedStore()]


class CreateNewRepositoryFileInput(CommitableBaseModel):
    """
    Create a new file in the repository.
    """

    file_path: str = Field(..., description="The path within the repository where the new file will be created.")
    file_content: str = Field(..., description="The content of the new repository file.")
    store: Annotated[Any, InjectedStore()]


class RenameRepositoryFileInput(CommitableBaseModel):
    """
    Rename a file in the repository.
    """

    file_path: str = Field(..., description="The path of the file to be renamed within the repository.")
    new_file_path: str = Field(..., description="The new path and name for the file.")
    store: Annotated[Any, InjectedStore()]


class DeleteRepositoryFileInput(CommitableBaseModel):
    """
    Delete a file in the repository.
    """

    file_path: str = Field(..., description="The path of the file to delete within the repository.")
    store: Annotated[Any, InjectedStore()]


class RunCommandResult(BaseModel):
    """
    The result of running a command in the sandbox.
    """

    command: str
    output: str
    exit_code: int


class RunCommandInput(BaseModel):
    """
    Run a command in the sandbox.
    """

    commands: list[str] = Field(..., description="The commands to run in the sandbox.")
    intent: str = Field(..., description=("A description of why you're running these commands."))
    store: Annotated[Any, InjectedStore()]


class RunCommandResponse(BaseModel):
    """
    The response from running commands in the sandbox.
    """

    results: list[RunCommandResult]
    archive: Base64Bytes | None


class RunCodeInput(BaseModel):
    """
    Run python code.
    """

    dependencies: list[str] = Field(
        default_factory=list, description="The dependencies to install before running the code."
    )
    python_code: str = Field(
        ...,
        description=(
            "The python code to be evaluated. The contents will be in main.py. "
            "Tip: Use the `print` function to output results."
        ),
    )
    intent: str = Field(..., description=("A description of why you're running this code."))


class WebSearchInput(BaseModel):
    """
    Perform a web search to retrieve up-to-date information.
    """

    query: str = Field(
        ...,
        description=(
            "Search query to look up relevant information in the web. "
            "The query should be focused and specific to get accurate results."
        ),
    )
    intent: str = Field(
        ...,
        description=(
            "A brief description of why you are performing this web search and how it relates to the user's request."
        ),
    )
