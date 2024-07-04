from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from codebase.clients import RepoClient


class ClientType(StrEnum):
    GITLAB = "gitlab"
    GITHUB = "github"


class Repository(BaseModel):
    pk: int
    slug: str
    name: str
    default_branch: str
    client: ClientType
    head_sha: str
    topics: list[str] = Field(default_factory=list)

    @classmethod
    def load_from_repo(cls, repo_client: RepoClient, repo_id: str):
        return repo_client.get_repository(repo_id)


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
    source_branch: str


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


class FileChangeAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    MOVE = "move"


class FileChange(BaseModel):
    action: FileChangeAction
    file_path: str
    content: str | None = None
    previous_path: str | None = None
    commit_messages: list[str] = []


class User(BaseModel):
    id: int
    name: str
    username: str


class IssueNote(BaseModel):
    author: User
    body: str


class Issue(BaseModel):
    id: int
    title: str
    description: str
    state: str
    notes: list[IssueNote] = Field(default_factory=list)
    related_merge_requests: list[MergeRequest] = Field(default_factory=list)
