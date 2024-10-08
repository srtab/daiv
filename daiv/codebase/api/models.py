from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

LABEL_DAIV = "daiv"


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
    description: str
    state: str
    assignee_id: int | None
    action: IssueAction
    labels: list[Label]

    def is_daiv(self) -> bool:
        """
        Check if the issue is a DAIV issue
        """
        return any(label.title == LABEL_DAIV for label in self.labels)


class MergeRequest(BaseModel):
    """
    Gitlab Merge Request
    """

    id: int
    iid: int
    title: str
    description: str
    state: str
    work_in_progress: bool
    source_branch: str
    target_branch: str
    assignee_id: int | None
    labels: list[Label]

    def is_daiv(self) -> bool:
        """
        Check if the merge request is a DAIV merge request
        """
        return any(label.title == LABEL_DAIV for label in self.labels)


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
    default_branch: str


class User(BaseModel):
    """
    Gitlab User
    """

    id: int
    name: str
    username: str
    email: str
