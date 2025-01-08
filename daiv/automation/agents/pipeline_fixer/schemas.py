from typing import Literal

from pydantic import BaseModel, Field


class PipelineLogClassifierOutput(BaseModel):
    """
    Classify the root cause of the failure.
    """

    # model_config = ConfigDict(title="pipeline_log_classifier")  # noqa: ERA001

    category: Literal["codebase", "external-factor"] = Field(
        ...,
        description="State whether the issue is 'codebase' or 'external-factor'. "
        "If there is any possibility that the issue could be caused by an external factor, "
        "classify it as 'external-factor'.",
    )
    pipeline_phase: Literal["lint", "unittest", "other"] = Field(
        ...,
        description="Identified the phase when the pipeline failed according to job_logs output. "
        "If the command and command output is related with unittests failing, state it as 'unittest'. "
        "If it is related with linting, state it as 'lint'. Otherwise as 'other'.",
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
        "Use markdown formatting for `variables`, `files`, `directories`, `dependencies`, etc.",
    )


class ActionPlan(BaseModel):
    """
    A plan to fix an issue.
    """

    description: str = Field(..., description="Briefly describe the issue.")
    steps: list[str] = Field(..., description="A list of steps to fix the issue.")


class ActionPlanOutput(BaseModel):
    """
    Provide the plan to fix an issue based on the root cause with this tool.
    Only use this tool to provide the plan at the end of the response.
    """

    actions: list[ActionPlan] = Field(..., description="A list of actions to fix the issue.")
