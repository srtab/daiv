from pydantic import BaseModel, Field


class SnippetReplacerOutput(BaseModel):
    content: str = Field(description="The content of the resulting snippet after replacement.")
