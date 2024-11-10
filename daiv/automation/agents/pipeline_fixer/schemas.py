from typing import Literal

from pydantic import BaseModel, Field


class PipelineLogClassifierOutput(BaseModel):
    category: Literal["codebase", "external-factor"] = Field(
        ...,
        description="State whether the issue is 'codebase' or 'external-factor'. "
        "If there is any possibility that the issue could be caused by an external factor, "
        "classify it as 'external-factor'.",
    )
    category_reasoning: str = Field(
        ...,
        description="Explain the reasoning behind your categorization. "
        "Consider the potential external factors that could have caused the failure.",
    )
