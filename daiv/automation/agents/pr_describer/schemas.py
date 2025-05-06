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
            "Create a self-explanatory title that describes what the pull request does. "
            "Derive solely from the supplied changes (no external context). "
            "Sentence-case, imperative mood. No more than 72 characters."
        )
    )
    branch: str = Field(
        description="The branch name associated with the changes. If the input already contains a branch name, use it.",
        pattern=r"[a-z0-9-_/]",
    )
    description: str = Field(
        description=(
            "Detail what was changed, why it was changed, and how it was changed. "
            "Summarize functional impact **only from what is given**. "
            "No speculation or inferred context."
            "Refer always to the changes and never to the pull request."
        )
    )
    summary: list[str] = Field(
        description=(
            "Concise bulleted description of the pull request."
            "Start each bullet with `Add`, `Update`, `Fix`, `Remove`, etc."
            "Group similar operations; avoid redundancy; imperative mood."
            "Markdown format `variables`, `files`, and `directories` like this."
        )
    )
    commit_message: str = Field(description="Commit message, short and concise.")
