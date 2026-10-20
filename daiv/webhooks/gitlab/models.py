from enum import StrEnum
from typing import Literal

from ninja import Field
from pydantic import BaseModel


class MergeRequestAction(StrEnum):
    """
    Gitlab Merge Request Action
    """

    MERGE = "merge"
    OPEN = "open"
    CLOSE = "close"
    REOPEN = "reopen"
    UPDATE = "update"
    APPROVED = "approved"
    UNAPPROVED = "unapproved"


class IssueAction(StrEnum):
    """
    Gitlab Issue Action
    """

    OPEN = "open"
    UPDATE = "update"
    REOPEN = "reopen"
    CLOSE = "close"


class Label(BaseModel):
    title: str


class LabelChange(BaseModel):
    """
    GitLab Label Change - represents previous and current label lists
    """

    previous: list[Label] = Field(default_factory=list)
    current: list[Label] = Field(default_factory=list)


class IssueChanges(BaseModel):
    """
    GitLab Issue Changes - tracks what changed in an issue update event
    """

    labels: LabelChange | None = None


class Issue(BaseModel):
    """
    Gitlab Issue
    """

    id: int
    iid: int
    title: str
    description: str | None
    state: str
    assignee_id: int | None
    action: IssueAction | None = None
    labels: list[Label]
    type: Literal["Issue", "Task"]

    @property
    def label_names(self) -> list[str]:
        return [label.title for label in self.labels]


class MergeRequest(BaseModel):
    """
    Gitlab Merge Request
    """

    id: int
    iid: int
    title: str
    description: str | None = None
    state: str
    work_in_progress: bool | None = None
    source_branch: str
    target_branch: str
    assignee_id: int | None = None
    labels: list[Label] = Field(default_factory=list)

    @property
    def label_names(self) -> list[str]:
        return [label.title for label in self.labels]


class NoteableType(StrEnum):
    """
    Gitlab Noteable Type
    """

    ISSUE = "Issue"
    MERGE_REQUEST = "MergeRequest"


class NoteDiffPosition(BaseModel):
    """
    Gitlab Note Diff Position
    """

    type: Literal["new", "old", "expanded"] | None
    old_line: int | None
    new_line: int | None


class NotePositionLineRange(BaseModel):
    """
    Gitlab Note Position Line Range
    """

    start: NoteDiffPosition
    end: NoteDiffPosition


class NotePositionType(StrEnum):
    """
    Gitlab Note Position Type
    """

    TEXT = "text"
    FILE = "file"


class NotePosition(BaseModel):
    """
    Gitlab Note Position
    """

    head_sha: str
    old_path: str
    new_path: str
    position_type: NotePositionType
    old_line: int | None = None
    new_line: int | None = None
    line_range: NotePositionLineRange | None = None


class NoteAction(StrEnum):
    """
    Gitlab Note Action
    """

    UPDATE = "update"
    CREATE = "create"


class Note(BaseModel):
    """
    Gitlab Note
    """

    id: int
    action: NoteAction
    noteable_type: NoteableType
    noteable_id: int
    discussion_id: str
    note: str
    system: bool
    type: Literal["DiffNote", "DiscussionNote", "Note"] | None = None
    position: NotePosition | None = None


class Project(BaseModel):
    """
    Gitlab Project
    """

    id: int
    path_with_namespace: str
    default_branch: str | None = None


class User(BaseModel):
    """
    Gitlab User
    """

    id: int
    name: str
    username: str
    email: str


class PipelineEvent(BaseModel):
    id: int
    iid: int | None = None
    ref: str
    sha: str
    status: str


class MergeRequestEvent(BaseModel):
    """
    Gitlab Merge Request event object_attributes from the merge_request webhook.
    """

    id: int
    iid: int
    title: str
    state: Literal["opened", "closed", "merged", "locked"]
    action: MergeRequestAction | None = None
    source_branch: str
    target_branch: str
    author_id: int
    merged_at: str | None = None
