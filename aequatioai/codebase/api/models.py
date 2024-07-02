from enum import StrEnum

from pydantic import BaseModel


class IssueAction(StrEnum):
    """
    Gitlab Issue Action
    """

    OPEN = "open"
    UPDATE = "update"
    REOPEN = "reopen"
    CLOSE = "close"


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


class NoteableType(StrEnum):
    """
    Gitlab Noteable Type
    """

    ISSUE = "issue"
    MERGE_REQUEST = "merge_request"


class Note(BaseModel):
    """
    Gitlab Note
    """

    id: int
    noteable_type: NoteableType
    noteable_id: int
    body: str
    system: bool


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
