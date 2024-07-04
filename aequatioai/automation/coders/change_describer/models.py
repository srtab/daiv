from pydantic import BaseModel, Field


class ChangesDescription(BaseModel):
    branch: str = Field(description="The branch name for the pull request. Must starts with 'feat/' or 'fix/'.")
    title: str = Field(description="Title with a short and concise description of the changes for the pull request.")
    description: str = Field(description="Description with a detailed explanation of the changes for the pull request.")
    commit_message: str = Field(description="Commit message, short and concise.")
