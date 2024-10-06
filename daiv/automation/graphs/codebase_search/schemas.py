from pydantic import BaseModel, Field


class GradeDocumentsOutput(BaseModel):
    """
    Binary score for relevance check on retrieved documents.
    """

    binary_score: bool = Field(description="Documents are relevant to the query. True if relevant, False otherwise.")


class ImprovedQueryOutput(BaseModel):
    """
    Represents a better query.
    """

    query: str = Field(description="The improved query.")
