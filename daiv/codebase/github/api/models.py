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


class Installation(BaseModel):
    """
    GitHub Installation
    """

    id: int


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

    def is_daiv(self) -> bool:
        """
        Check if the issue is a DAIV issue
        """
        return any(label["name"].lower() == BOT_LABEL for label in self.labels) or self.title.lower().startswith(
            BOT_LABEL
        )


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
