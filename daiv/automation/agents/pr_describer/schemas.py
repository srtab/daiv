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
    title: str = Field(
        description=(
            "Self-explanatory PR title derived only from supplied changes. Sentence-case, imperative mood. Max 72 chars"
        )
    )
    branch: str = Field(
        description="Branch name to create. Must follow allowed characters and (separately) any provided convention.",
        pattern=r"[a-z0-9-_/]",
    )
    description: str = Field(
        description=(
            "Detail what was changed, why it was changed, and how it was changed. "
            "Summarize functional impact **only from what is given**. No speculation or inferred context."
            "Refer always to the changes and never to the pull request."
            "Use markdown to structure clearly the description to be simple to understand and read."
        )
    )
    summary: list[str] = Field(
        description=(
            "Concise bulleted description of the pull request, like a changelog."
            "Start each bullet with `Add`, `Update`, `Fix`, `Remove`, etc."
            "Group similar operations; avoid redundancy; imperative mood."
            "Markdown format `variables`, `files`, and `directories` like this."
        )
    )
    commit_message: str = Field(description="Commit message, short and concise, on one sentence.")
