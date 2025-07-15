from typing import Literal

from pydantic import BaseModel, Field
from typing_extensions import TypedDict


class TroubleshootingDetail(BaseModel):
    """
    Provide a detailed explanation of your troubleshooting findings.
    """

    title: str = Field(description="A short title for the troubleshooting detail.")
    file_path: str = Field(description="The path to the file that is causing the issue, if applicable.", default="")
    category: Literal["codebase", "external-factor", "other"] = Field(
        description=(
            "State whether the issue is 'codebase' or 'external-factor'. "
            "If there is any possibility that the issue could be caused by an external factor, "
            "classify it as 'external-factor'. "
            "If the issue is not related to codebase or external-factor, classify it as 'other'."
        )
    )
    details: str = Field(
        description=(
            "Summary of your key troubleshooting findings. "
            "- Use the safe format: fenced with tildes `~~~language` … `~~~` for markdown code blocks; "
            "- Use markdown formatting (e.g., for `variables`, `files`, `directories`, `dependencies`) as needed."
        )
    )
    remediation_steps: list[str] = Field(
        description=(
            "Outline actionable remediation steps with instructions to resolve the identified external-factor issues, "
            "including any code changes, configuration adjustments, or infrastructure interventions. "
            "Focus on actions that solve the identified external-factor issue directly. "
            "If the issue is not related to external-factor, do not provide any remediation steps."
        ),
        default_factory=list,
    )


class CommandOuputInput(TypedDict):
    output: str


class CommandOuputEvaluation(BaseModel):
    """
    Result of the command output analysis to determine if there are any errors, or indications of failures.
    """

    has_errors: bool = Field(description="Whether the command output contains any errors, or indications of failures.")
