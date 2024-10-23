from pydantic import BaseModel, Field


class PullRequestDescriberOutput(BaseModel):
    branch: str = Field(
        description=(
            "The branch name. No spaces are allowed in branch names. "
            "You can use numbers, hyphens (-), underscores (_), lowercase letters from the ASCII standard table "
            "or forward slashes (/)."
        )
    )
    title: str = Field(description="Title with no more than 10 words and concise description of the changes.")
    description: str = Field(description=("Description of the functional changes."))
    summary: list[str] = Field(
        description=(
            "Concise bulleted description of the pull request."
            "Markdown format `variables`, `files`, and `directories` like this."
        )
    )
    commit_message: str = Field(description="Commit message, short and concise.")
