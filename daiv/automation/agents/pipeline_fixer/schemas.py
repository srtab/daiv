from typing import Literal

from pydantic import BaseModel, Field


class PipelineLogClassifierOutput(BaseModel):
    """
    Classify the root cause of the failure.
    """

    # model_config = ConfigDict(title="pipeline_log_classifier")

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
    root_cause: str = Field(
        ...,
        description="A detailed explanation of the primary cause behind the failure. "
        "If related to the 'codebase', describe any code elements potentially contributing to the problem. "
        "For 'external-factor' issues, clarify which external factors may have impacted functionality. "
        "Use Markdown formatting if necessary for enhanced clarity.",
    )
