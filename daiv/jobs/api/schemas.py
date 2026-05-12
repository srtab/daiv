from datetime import datetime  # noqa: TC003 - required at runtime by Pydantic
from typing import Literal

from ninja import Field, Schema
from notifications.choices import NotifyOn  # noqa: TC002 - required at runtime by Pydantic


class RepoSubmitItem(Schema):
    repo_id: str = Field(min_length=1)
    ref: str | None = None


class JobSubmitRequest(Schema):
    repos: list[RepoSubmitItem] = Field(min_length=1, max_length=20)
    prompt: str = Field(min_length=1)
    use_max: bool = False
    notify_on: NotifyOn | None = None


class JobSubmitJobItem(Schema):
    job_id: str
    repo_id: str
    ref: str | None = None


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
    status: Literal["READY", "RUNNING", "SUCCESSFUL", "FAILED"]
    result: str | None = None
    merge_request_url: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
