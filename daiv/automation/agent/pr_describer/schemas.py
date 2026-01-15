from __future__ import annotations

from pydantic import BaseModel, Field


class PullRequestMetadata(BaseModel):
    title: str = Field()
    branch: str = Field(pattern=r"[a-z0-9-_/]")
    description: str = Field()
    commit_message: str = Field()
