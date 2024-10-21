from pydantic import BaseModel, Field


class PullRequestDescriberOutput(BaseModel):
    branch: str = Field(description="The branch name.")
    title: str = Field(description="Title with a short and concise description of the changes.")
    description: str = Field(description=("Description of the functional changes."))
    summary: list[str] = Field(
        description=(
            "Concise bulleted description of the pull request."
            "Markdown format `variables`, `files`, and `directories` like this."
        )
    )
    commit_message: str = Field(description="Commit message, short and concise.")
