import textwrap

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field


class PullRequestMetadata(BaseModel):
    branch: str = Field(description="The branch name for the pull request. Must starts with 'feat/' or 'fix/'.")
    title: str = Field(description="Title with a short and concise description of the changes for the pull request.")
    description: str = Field(description="Description with a detailed explanation of the changes for the pull request.")
    commit_message: str = Field(description="Commit message, short and concise.")


prompt_template = ChatPromptTemplate.from_messages([
    (
        "system",
        textwrap.dedent(
            """\
            Act as an exceptional senior software engineer that is specialized in describing changes.
            """
        ),
    ),
    ("user", "Describe the following changes:\n {changes}"),
])

model = ChatOpenAI(model="gpt-4o-mini", temperature=0)

chain = prompt_template | model.with_structured_output(PullRequestMetadata)

result = chain.invoke({"changes": "- Added a new feature\n- Fixed a bug"})
