from __future__ import annotations

import difflib
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, computed_field, field_validator

from core.constants import BOT_LABEL


class ClientType(StrEnum):
    GITLAB = "gitlab"
    GITHUB = "github"


class Repository(BaseModel):
    pk: int
    slug: str
    name: str
    default_branch: str
    client: ClientType
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
    sha: str | None = None

    def is_daiv(self) -> bool:
        return any(label.lower() == BOT_LABEL for label in self.labels) or self.title.lower().startswith(BOT_LABEL)


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
    previous_path: str | None = None
    original_content: str = Field(default="", exclude=True)
    content: str = Field(default="", exclude=True)

    def to_markdown(self):
        if self.action == FileChangeAction.CREATE:
            return f"Created `{self.file_path}`"
        elif self.action == FileChangeAction.UPDATE:
            return f"Updated `{self.file_path}`"
        elif self.action == FileChangeAction.DELETE:
            return f"Deleted `{self.file_path}`"
        elif self.action == FileChangeAction.MOVE:  # pragma: no cover
            return f"Renamed `{self.previous_path}` to `{self.file_path}`"

    @computed_field(return_type=str, repr=False)
    def diff_hunk(self) -> str:
        """
        Get the diff hunk for the file change.
        """
        diff_from_file = f"a/{self.previous_path or self.file_path}"
        diff_to_file = f"b/{self.file_path}"

        if self.action == FileChangeAction.CREATE:
            diff_from_file = "a/dev/null"
        if self.action == FileChangeAction.DELETE:
            diff_to_file = "a/dev/null"

        diff_hunk = difflib.unified_diff(
            self.original_content.splitlines(),
            self.content.splitlines(),
            fromfile=diff_from_file,
            tofile=diff_to_file,
            lineterm="",
        )
        return "\n".join(diff_hunk)


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


class Discussion(BaseModel):
    id: str
    resolve_id: str | None = None  # The id of the comment to resolve, only used for GitHub.
    notes: list[Note] = Field(default_factory=list)
    is_thread: bool = False
    is_resolvable: bool = False


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

    @field_validator("title", mode="after")
    @classmethod
    def clean_title(cls, value: str) -> str:
        """
        Clean the title of the issue by removing the bot label and the colon if it exists.

        This will avoid issues with agents as they will think the bot label is part of the context for the task.
        """
        if value.lower().startswith(f"{BOT_LABEL}:"):
            return value[len(BOT_LABEL) + 1 :].strip()
        elif value.lower().startswith(BOT_LABEL):
            return value[len(BOT_LABEL) :].strip()
        return value
