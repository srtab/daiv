from pydantic import BaseModel, Field


class ChangesDescription(BaseModel):
    branch: str = Field(description="The branch name.")
    title: str = Field(description="Title for the pull request.")
    description: str = Field(description="Description for the pull request.")
    commit_message: str = Field(description="Commit message, short and concise.")
