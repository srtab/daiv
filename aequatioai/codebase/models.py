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


class MergeRequestDiff(BaseModel):
    repo_id: str
    merge_request_id: str
    ref: str
    old_path: str
    new_path: str
    diff: bytes
    new_file: bool = False
    renamed_file: bool = False
    deleted_file: bool = False


class FileChange(BaseModel):
    action: Literal["create", "update", "delete", "move"]
    file_path: str
    content: str | None = None
    previous_path: str | None = None
    commit_messages: list[str] = []
