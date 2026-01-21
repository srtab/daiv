from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from core.constants import BOT_AUTO_LABEL, BOT_MAX_LABEL


class GitPlatform(StrEnum):
    GITLAB = "gitlab"
    GITHUB = "github"
    SWE = "swe"


class Repository(BaseModel):
    pk: int
    slug: str
    name: str
    clone_url: str
    default_branch: str
    git_platform: GitPlatform
    topics: list[str] = Field(default_factory=list)


class RepositoryFile(BaseModel):
    repo_id: str
    file_path: str
    ref: str | None = None
    content: str | None = None


class Job(BaseModel):
    id: int
    name: str
    status: Literal["created", "pending", "running", "failed", "success", "canceled", "skipped", "manual", "scheduled"]
    stage: str
    allow_failure: bool
    failure_reason: str | None = None

    def is_failed(self) -> bool:
        return self.status == "failed"


class Pipeline(BaseModel):
    id: int
    iid: int | None = None
    sha: str
    status: str
    web_url: str
    jobs: list[Job] = Field(default_factory=list)


class MergeRequest(BaseModel):
    repo_id: str
    merge_request_id: int
    source_branch: str
    target_branch: str
    title: str
    description: str
    labels: list[str] = Field(default_factory=list)
    web_url: str | None = None
    sha: str | None = None
    author: User


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


class User(BaseModel):
    id: int
    name: str | None = None
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


class NoteLineInfo(BaseModel):
    side: Literal["source", "target"]
    line_no: int


class NotePosition(BaseModel):
    head_sha: str
    old_path: str
    new_path: str
    position_type: NotePositionType
    old_line: int | None = None
    new_line: int | None = None
    line_range: NotePositionLineRange | None = None
    line_info: NoteLineInfo | None = None


class NoteType(StrEnum):
    DIFF_NOTE = "DiffNote"
    DISCUSSION_NOTE = "DiscussionNote"


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


class SimpleDiscussion(BaseModel):
    id: str
    resolve_id: str | None = None  # The id of the comment to resolve, only used for GitHub.
    is_thread: bool = False
    is_resolvable: bool = False


class Discussion(SimpleDiscussion):
    notes: list[Note] = Field(default_factory=list)

    def as_simple(self) -> SimpleDiscussion:
        return SimpleDiscussion(
            id=self.id, resolve_id=self.resolve_id, is_thread=self.is_thread, is_resolvable=self.is_resolvable
        )


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
    author: User
    issue_type: IssueType = IssueType.ISSUE
    notes: list[Note] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)

    def has_max_label(self) -> bool:
        """
        Check if the issue has the daiv-max label (case-insensitive).

        Returns:
            bool: True if the issue has the daiv-max label, False otherwise.
        """
        return any(label.lower() == BOT_MAX_LABEL.lower() for label in self.labels)

    def has_auto_label(self) -> bool:
        """
        Check if the issue has the daiv-auto label (case-insensitive).

        Returns:
            bool: True if the issue has the daiv-auto label, False otherwise.
        """
        return any(label.lower() == BOT_AUTO_LABEL.lower() for label in self.labels)
