from datetime import datetime  # noqa: TC003 - required at runtime by Pydantic
from typing import Any, Literal
from uuid import UUID  # noqa: TC003 - required at runtime by Pydantic

from ninja import Field, Schema
from pydantic import ConfigDict

from codebase.references import MAX_REFS_PER_SUBMISSION, RefIn  # noqa: TC001 - required at runtime by Ninja
from core.models import ThinkingLevelChoices  # noqa: TC001 - required at runtime by Ninja


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
    references: list[RefIn] = Field(
        default_factory=list,
        max_length=MAX_REFS_PER_SUBMISSION,
        description="External work-item references to link into the MR/PR.",
    )


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
    status: Literal["QUEUED", "READY", "RUNNING", "SUCCESSFUL", "WAITING_INPUT", "FAILED"]
    thread_id: str | None = None
    result: str | None = None
    question: dict[str, Any] | None = Field(
        default=None,
        description="When status is WAITING_INPUT: the questions the agent asked. Answer by submitting a job with "
        "this thread_id and the answer as the prompt.",
    )
    merge_request_url: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
