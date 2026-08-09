from datetime import datetime  # ruff: ignore[typing-only-standard-library-import] - required at runtime by Pydantic
from typing import Literal
from uuid import UUID  # ruff: ignore[typing-only-standard-library-import] - required at runtime by Pydantic

from ninja import Field, Schema
from pydantic import ConfigDict

from core.models import (
    ThinkingLevelChoices,  # ruff: ignore[typing-only-first-party-import] - required at runtime by Ninja
)


class RepoSubmitItem(Schema):
    repo_id: str = Field(min_length=1)
    ref: str | None = None


class JobSubmitRequest(Schema):
    # ``extra="forbid"`` so a stale client that still sends ``use_max`` (or any
    # other dropped field) gets a clear 422 instead of a silent strip and a
    # 202 that runs on the default model.
    model_config = ConfigDict(extra="forbid")

    repos: list[RepoSubmitItem] = Field(min_length=1, max_length=20)
    prompt: str = Field(min_length=1)
    agent_model: str | None = None
    agent_thinking_level: ThinkingLevelChoices | None = None
    muted: bool = Field(default=False, description="Mute notifications for every job in this batch.")
    environment: str | None = None
    thread_id: UUID | None = None


class JobSubmitJobItem(Schema):
    job_id: str
    repo_id: str
    ref: str | None = None
    thread_id: str
    status: Literal["QUEUED", "READY"]


class JobSubmitFailureItem(Schema):
    repo_id: str
    ref: str
    error: str


class JobSubmitResponse(Schema):
    batch_id: str
    jobs: list[JobSubmitJobItem]
    failed: list[JobSubmitFailureItem]


class JobStatusResponse(Schema):
    job_id: str
    status: Literal["QUEUED", "READY", "RUNNING", "SUCCESSFUL", "FAILED"]
    thread_id: str | None = None
    result: str | None = None
    merge_request_url: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
