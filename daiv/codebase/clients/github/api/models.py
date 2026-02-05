from typing import Literal

from pydantic import BaseModel
from pydantic.fields import Field

from core.constants import BOT_AUTO_LABEL, BOT_LABEL, BOT_MAX_LABEL


class User(BaseModel):
    """
    GitHub User
    """

    id: int
    name: str | None = None
    username: str = Field(alias="login")


class Repository(BaseModel):
    """
    GitHub Repository
    """

    id: int
    full_name: str
    default_branch: str


class Issue(BaseModel):
    """
    GitHub Issue
    """

    id: int
    number: int
    title: str
    state: Literal["open", "closed"]
    labels: list[dict] = Field(default_factory=list)
    pull_request: dict | None = None
    draft: bool = False

    def is_daiv(self) -> bool:
        """
        Check if the issue is a DAIV issue by checking for any DAIV label (daiv, daiv-auto, daiv-max).
        """
        daiv_labels = {BOT_LABEL.lower(), BOT_AUTO_LABEL.lower(), BOT_MAX_LABEL.lower()}
        return any(label["name"].lower() in daiv_labels for label in self.labels)

    def is_pull_request(self) -> bool:
        """
        Check if the issue is a pull request.
        """
        return self.pull_request is not None

    def is_issue(self) -> bool:
        """
        Check if the issue is an issue.
        """
        return not self.is_pull_request()


class Label(BaseModel):
    """
    GitHub Label
    """

    id: int
    name: str


class IssueChange(BaseModel):
    """
    GitHub Issue Change
    """

    from_value: str = Field(default="", alias="from")


class IssueChanges(BaseModel):
    """
    GitHub Issue Changes
    """

    title: IssueChange
    body: IssueChange


class Comment(BaseModel):
    """
    GitHub Comment
    """

    id: int
    body: str
    user: User


class Review(BaseModel):
    """
    GitHub Review
    """

    id: int
    user: User
    body: str | None = None


class Ref(BaseModel):
    """
    GitHub Ref
    """

    ref: str
    sha: str


class PullRequest(BaseModel):
    """
    GitHub Pull Request
    """

    id: int
    number: int
    title: str
    state: Literal["open", "closed"]
    draft: bool = False
    head: Ref
    base: Ref
