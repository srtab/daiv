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
    merge_request_id: int
    source_branch: str
    target_branch: str
    title: str
    description: str
    labels: list[str] = Field(default_factory=list)


class MergeRequestDiff(BaseModel):
    repo_id: str
    merge_request_id: int
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


class CodebaseChanges(BaseModel):
    file_changes: dict[str, FileChange] = Field(default_factory=dict)


class User(BaseModel):
    id: int
    name: str
    username: str


class NoteableType(StrEnum):
    """
    Gitlab Noteable Type
    """

    ISSUE = "Issue"
    MERGE_REQUEST = "MergeRequest"


class NoteDiffPositionType(StrEnum):
    OLD = "old"
    NEW = "new"
    EXPANDED = "expanded"


class NoteDiffPosition(BaseModel):
    type: NoteDiffPositionType | None
    old_line: int | None
    new_line: int | None


class NotePositionLineRange(BaseModel):
    start: NoteDiffPosition
    end: NoteDiffPosition


class NotePositionType(StrEnum):
    TEXT = "text"
    FILE = "file"


class NotePosition(BaseModel):
    head_sha: str
    old_path: str
    new_path: str
    position_type: NotePositionType
    old_line: int | None = None
    new_line: int | None = None
    line_range: NotePositionLineRange | None = None


class NoteType(StrEnum):
    DIFF_NOTE = "DiffNote"
    DISCUSSION_NOTE = "DiscussionNote"
    NOTE = "Note"


class Note(BaseModel):
    id: int
    body: str
    author: User
    noteable_type: NoteableType
    system: bool
    resolvable: bool
    resolved: bool | None = None
    type: NoteType | None = None
    position: NotePosition | None = None
    hunk: str | None = None


class Discussion(BaseModel):
    id: str
    notes: list[Note] = Field(default_factory=list)


class IssueType(StrEnum):
    ISSUE = "issue"
    TASK = "task"


class Issue(BaseModel):
    id: int | None = None
    iid: int | None = None
    title: str
    description: str | None = None
    state: str | None = None
    assignee: User | None = None
    issue_type: IssueType = IssueType.ISSUE
    has_tasks: bool = False
    notes: list[Note] = Field(default_factory=list)
    related_merge_requests: list[MergeRequest] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
