from __future__ import annotations

from typing import TYPE_CHECKING, NotRequired, TypedDict

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from codebase.base import FileChange


class PullRequestDescriberInput(TypedDict):
    changes: list[FileChange]
    extra_context: NotRequired[str]
    branch_name_convention: NotRequired[str]


class PullRequestMetadata(BaseModel):
    branch: str = Field(description=("The branch name associated with the changes."))
    title: str = Field(
        description="Create a self-explanatory title that describes what the pull request does.", max_length=72
    )
    description: str = Field(description="Detail what was changed, why it was changed, and how it was changed.")
    summary: list[str] = Field(
        description=(
            "Concise bulleted description of the pull request."
            "Markdown format `variables`, `files`, and `directories` like this."
        )
    )
    commit_message: str = Field(description="Commit message, short and concise.")
