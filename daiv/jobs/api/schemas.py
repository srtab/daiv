from typing import TYPE_CHECKING, Literal

from ninja import Field, Schema

if TYPE_CHECKING:
    from datetime import datetime


class JobSubmitRequest(Schema):
    repo_id: str = Field(min_length=1)
    ref: str | None = None
    prompt: str = Field(min_length=1)


class JobSubmitResponse(Schema):
    job_id: str


class JobStatusResponse(Schema):
    job_id: str
    status: Literal["READY", "RUNNING", "SUCCESSFUL", "FAILED"]
    result: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
