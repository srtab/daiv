from datetime import datetime  # noqa: TC003 - required at runtime by Pydantic
from typing import Literal

from ninja import Field, Schema
from notifications.choices import NotifyOn  # noqa: TC002 - required at runtime by Pydantic


class RepoSubmitItem(Schema):
    repo_id: str = Field(min_length=1)
    ref: str | None = None


_UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class JobSubmitRequest(Schema):
    repos: list[RepoSubmitItem] = Field(min_length=1, max_length=20)
    prompt: str = Field(min_length=1)
    use_max: bool = False
    notify_on: NotifyOn | None = None
    environment: str | None = None
    thread_id: str | None = Field(default=None, min_length=36, max_length=36, pattern=_UUID_PATTERN)


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
