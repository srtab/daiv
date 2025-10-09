from typing import Literal

from pydantic import BaseModel
from pydantic.fields import Field

from core.constants import BOT_LABEL


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
        Check if the issue is a DAIV issue.
        """
        return any(label["name"].lower() == BOT_LABEL for label in self.labels) or self.title.lower().startswith(
            BOT_LABEL
        )

    def is_pull_request(self) -> bool:
        """
        Check if the issue is a pull request.
        """
        return self.pull_request is not None


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
