from typing import Literal

from pydantic import BaseModel, Field


class ErrorLogEvaluation(BaseModel):
    """
    Provide the output of the error log evaluator to determine if the two logs are the same error using this tool.
    """

    is_same_error: bool = Field(description="Whether the two logs are the same error")
    justification: str = Field(description="The justification for the decision")


class TroubleshootingDetail(BaseModel):
    """
    Provide a detailed explanation of your troubleshooting findings.
    """

    details: str = Field(
        ...,
        description=(
            "Summary of your key troubleshooting findings. "
            "Use markdown formatting (e.g., for `variables`, `files`, `directories`, `dependencies`) as needed."
        ),
    )
    file_path: str = Field(description="The path to the file that is causing the issue, if applicable.", default="")
    remediation_steps: list[str] = Field(
        description=(
            "Outline actionable remediation steps to resolve the identified issues, "
            "including any code changes, configuration adjustments, or infrastructure interventions. "
            "Focus on actions that solve the identified issue directly."
        ),
        default_factory=list,
    )


class PipelineLogClassification(BaseModel):
    """
    Provide your final categorization and troubleshooting details by using this tool.
    """

    # model_config = ConfigDict(title="pipeline_log_classifier")  # noqa: ERA001

    category: Literal["codebase", "external-factor"] = Field(
        ...,
        description=(
            "State whether the issue is 'codebase' or 'external-factor'. "
            "If there is any possibility that the issue could be caused by an external factor, "
            "classify it as 'external-factor'."
        ),
    )
    category_reasoning: str = Field(
        ...,
        description=(
            "Explain the reasoning behind your categorization, including any external factors that may have "
            "contributed to the failure."
        ),
    )
    pipeline_phase: Literal["lint", "unittest", "other"] = Field(
        ...,
        description=(
            "Identify the phase when the pipeline failed according to the job logs output. "
            "Use 'unittest' if the issue relates to unit tests, 'lint' for linting problems, "
            "or 'other' for any other phase."
        ),
    )
    troubleshooting: list[TroubleshootingDetail] = Field(
        description=(
            "A list of troubleshooting details for each identified error message. "
            "If there are no troubleshooting details, no error messages or the pipeline succeeded, "
            "return an empty list."
        ),
        default_factory=list,
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
