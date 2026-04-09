from datetime import datetime  # noqa: TC003 - required at runtime by Pydantic
from typing import Literal

from ninja import Field, Schema


class JobSubmitRequest(Schema):
    repo_id: str = Field(min_length=1)
    ref: str | None = None
    prompt: str = Field(min_length=1)
    use_max: bool = False


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
