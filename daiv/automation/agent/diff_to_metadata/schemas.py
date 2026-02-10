from __future__ import annotations

from pydantic import BaseModel, Field


class CommitMetadata(BaseModel):
    commit_message: str


class PullRequestMetadata(BaseModel):
    title: str
    branch: str = Field(pattern=r"[a-z0-9-_/]")
    description: str
