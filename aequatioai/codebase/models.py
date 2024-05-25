from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from codebase.clients import RepoClient


class RepositoryFile(BaseModel):
    repo_id: str
    file_path: str
    ref: str | None = None
    content: str | None = None

    @classmethod
    def load_from_repo(cls, repo_client: RepoClient, repo_id: str, file_path: str, ref: str | None = None):
        return cls(
            repo_id=repo_id,
            file_path=file_path,
            ref=ref,
            content=repo_client.get_repository_file(repo_id, file_path, ref=ref),
        )


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
