from __future__ import annotations

from typing import NotRequired, TypedDict

from pydantic import BaseModel, Field


class PullRequestDescriberInput(TypedDict):
    diff: str
    context_file_content: NotRequired[str]
    extra_context: NotRequired[str]


class PullRequestMetadata(BaseModel):
    title: str = Field()
    branch: str = Field(pattern=r"[a-z0-9-_/]")
    description: str = Field()
    commit_message: str = Field()
