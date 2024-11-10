from pydantic import BaseModel, Field


class PullRequestDescriberOutput(BaseModel):
    branch: str = Field(description=("The branch name associated with the changes."))
    title: str = Field(description="Title with no more than 10 words and concise description of the changes.")
    description: str = Field(
        description=(
            "Description of the functional changes. Refer always to the changes and not to the pull request, example: "
            "`The changes add a new function to calculate the area of a rectangle.` not "
            "`The pull request adds a new function to calculate the area of a rectangle.`"
        )
    )
    summary: list[str] = Field(
        description=(
            "Concise bulleted description of the pull request."
            "Markdown format `variables`, `files`, and `directories` like this."
        )
    )
    commit_message: str = Field(description="Commit message, short and concise.")
