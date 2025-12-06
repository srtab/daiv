from enum import StrEnum
from typing import Literal

from ninja import Field
from pydantic import BaseModel

from core.constants import BOT_AUTO_LABEL, BOT_LABEL, BOT_MAX_LABEL


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

    def is_daiv(self) -> bool:
        """
        Check if the issue is a DAIV issue by checking for any DAIV label (daiv, daiv-auto, daiv-max).
        """
        daiv_labels = {BOT_LABEL.lower(), BOT_AUTO_LABEL.lower(), BOT_MAX_LABEL.lower()}
        return any(label.title.lower() in daiv_labels for label in self.labels)


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

    def is_daiv(self) -> bool:
        """
        Check if the merge request is a DAIV merge request by checking for any DAIV label (daiv, daiv-auto, daiv-max).
        """
        daiv_labels = {BOT_LABEL.lower(), BOT_AUTO_LABEL.lower(), BOT_MAX_LABEL.lower()}
        return any(label.title.lower() in daiv_labels for label in self.labels)


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
