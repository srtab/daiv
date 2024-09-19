from textwrap import dedent

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field


class SnippetReplacerOutput(BaseModel):
    content: str = Field(description="The content of the resulting snippet after replacement.")


model = ChatOpenAI(model="gpt-4o-2024-08-06", temperature=0)

prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        dedent(
            """\
            Act as an exceptional senior software engineer that is responsible for code snippet replacement.
            It's absolutely vital that you completely and correctly execute your task.

            ### Guidelines ###
            - Ensure the code is valid and executable after applying replacement.
            - Maintain correct code padding, spacing, and indentation.
            - Do not make extraneous changes to the code or whitespace that are not related to the intent.
            - Do not just add a comment or leave a TODO; you must write functional code.
            - Carefully review your code and ensure it is formatted correctly.

            You must complete the task.
            """
        ),
    ),
    (
        "human",
        dedent(
            """\
            ### Tasks ###
            - Find the "original snippet" in the "code snippet" and replace it with the "replacement snippet".

            ### Original snippet ###
            {original_snippet}

            ### Replacement snippet ###
            {replacement_snippet}

            ### Code snippet  ###
            {content}
            """
        ),
    ),
])

snippet_replacer_agent = prompt | model.with_structured_output(SnippetReplacerOutput)
