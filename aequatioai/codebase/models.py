from typing import Literal

from pydantic import BaseModel


class RepositoryFile(BaseModel):
    repo_id: str
    file_path: str
    ref: str | None = None
    content: str | None = None


class MergeRequest(BaseModel):
    repo_id: str
    merge_request_id: str


class MergeRequestDiff(MergeRequest):
    repo_id: str
    merge_request_id: str
    old_path: str
    new_path: str
    diff: bytes | None = None
    new_file: bool = False
    renamed_file: bool = False
    deleted_file: bool = False


class FileChange(BaseModel):
    action: Literal["create", "update", "delete"]
    file_path: str
    content: str | None = None
    previous_path: str | None = None
