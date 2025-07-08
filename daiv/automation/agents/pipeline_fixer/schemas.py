from pydantic import BaseModel, Field
from typing_extensions import TypedDict


class TroubleshootingDetail(BaseModel):
    """
    Provide a detailed explanation of your troubleshooting findings.
    """

    title: str = Field(description="A short title to identify the issue.")
    details: str = Field(
        description=(
            "Summary of your key troubleshooting findings. "
            "- Use the safe format: fenced with tildes `~~~language` … `~~~` for markdown code blocks; "
            "- Use markdown formatting (e.g., for `variables`, `files`, `directories`, `dependencies`) as needed."
        )
    )
    file_path: str = Field(description="The path to the file that is causing the issue, if applicable.", default="")
    remediation_steps: list[str] = Field(
        description=(
            "Outline actionable remediation steps with instructions to resolve the identified issues, "
            "including any code changes, configuration adjustments, or infrastructure interventions. "
            "Focus on actions that solve the identified issue directly."
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


class CommandOuputInput(TypedDict):
    output: str


class CommandOuputEvaluation(BaseModel):
    """
    Result of the command output analysis to determine if there are any errors, or indications of failures.
    """

    has_errors: bool = Field(description="Whether the command output contains any errors, or indications of failures.")
