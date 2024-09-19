from textwrap import dedent

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field


class PullRequestDescriberOutput(BaseModel):
    branch: str = Field(description="The branch name for the pull request. Must starts with 'feat/' or 'fix/'.")
    title: str = Field(description="Title with a short and concise description of the changes for the pull request.")
    description: str = Field(
        description=(
            "Concise bulleted description of the pull request. "
            "Markdown format `variables`, `files`, and `directories` like this."
        )
    )
    commit_message: str = Field(description="Commit message, short and concise.")


model = ChatOpenAI(model="gpt-4o-mini-2024-07-18", temperature=0)

prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        dedent(
            """\
            Act as an exceptional senior software engineer that is specialized in describing changes.
            It's absolutely vital that you completely and correctly execute your task.
            """
        ),
    ),
    (
        "human",
        dedent(
            """\
            ### Task ###
            Write a pull request description that reflects all changes in this pull request. Here are the changes:

            {changes}
            """
        ),
    ),
])

pr_describer_agent = prompt | model.with_structured_output(PullRequestDescriberOutput)
