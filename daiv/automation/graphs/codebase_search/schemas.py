from pydantic import BaseModel, Field


class GradeDocumentsOutput(BaseModel):
    """
    Binary score for relevance check on retrieved documents.
    """

    binary_score: bool = Field(
        description="True if the code snippet is relevant to the query and its intent; False if it is not relevant."
    )


class ImprovedQueryOutput(BaseModel):
    """
    Represents a better query.
    """

    query: str = Field(description="The improved query.")
