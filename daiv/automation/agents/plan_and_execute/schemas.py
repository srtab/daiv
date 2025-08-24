from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict

from codebase.base import ClientType
from codebase.conf import settings
from core.utils import async_url_to_data_url, build_uri, is_valid_url

if TYPE_CHECKING:
    from langchain_core.messages import AnyMessage


FINALIZE_WITH_PLAN_DESCRIPTION = """\
FINALIZER — Deliver a self-contained implementation plan that satisfies the user's request.

Call this ONLY after completing Steps 0-1. Preconditions you MUST have satisfied earlier in this conversation:
(1) you have called `think` at least once in Step 0, and
(2) you have executed ≥1 inspection tool from {`repository_structure`, `search_code_snippets`, `retrieve_file_content`} to gather evidence.
If either is false, do NOT call this tool; instead continue the workflow or use `post_inspection_clarify_final` if ambiguity remains after inspection.

Requirements for the plan:
- Ordered list of granular ChangeInstructions in execution order.
- Related instructions that touch the same file appear consecutively to aid batching/review.
- Self-contained: no external URLs; embed essential snippets/data (short snippets only) using safe fences.
- Reference concrete files/functions/config keys discovered during inspection."""  # noqa: E501

FINALIZE_WITH_TARGETED_QUESTIONS_DESCRIPTION = """\
FINALIZER — targeted clarification questions asked ONLY after completing Steps 0-1.

Preconditions you MUST have satisfied earlier in this conversation:
(1) you have called `think` at least once in Step 0, and
(2) you have executed ≥1 inspection tool from {`repository_structure`, `search_code_snippets`, `retrieve_file_content`} attempting to resolve the ambiguity.
If either is false, do NOT call this tool.

Use this tool when ambiguity remains after inspection, when any required execution detail is still missing, or when external sources are conflicting."""  # NOQA: E501


class ImageURLTemplate(BaseModel):
    url: str = Field(description="URL of the image.")
    detail: str | None = Field(description="Detail of the image.", default=None)


class ImageTemplate(BaseModel):
    type: Literal["image", "text"]
    image_url: ImageURLTemplate

    @staticmethod
    async def from_images(
        images: list[Image],
        repo_client_slug: ClientType | None = None,
        project_id: int | None = None,
        text: str | None = None,
    ) -> list[ImageTemplate]:
        """
        Create a list of image templates from a list of images.

        Args:
            project_id (int): The project ID.
            images (list[Image]): The list of images.
            text (str): The text of the user request.

        Returns:
            list[ImageTemplate]: The list of image templates.
        """
        image_templates = []

        for image in images:
            image_url = None

            if is_valid_url(image.url):
                image_url = image.url

            elif (
                (parsed_url := urlparse(image.url))
                and not parsed_url.netloc
                and not parsed_url.scheme
                and repo_client_slug == ClientType.GITLAB
                and project_id
                and parsed_url.path.startswith(("/uploads/", "uploads/"))
            ):
                assert settings.GITLAB_AUTH_TOKEN is not None, "GitLab auth token is not set"
                _repo_image_url = build_uri(f"{settings.GITLAB_URL}api/v4/projects/{project_id}/", image.url)
                image_url = await async_url_to_data_url(
                    _repo_image_url, headers={"PRIVATE-TOKEN": settings.GITLAB_AUTH_TOKEN.get_secret_value()}
                )

            if image_url:
                image_templates.append(
                    ImageTemplate(type="image", image_url=ImageURLTemplate(url=image_url)).model_dump(exclude_none=True)
                )

        return image_templates


class Image(BaseModel):
    url: str = Field(description="URL of the image.")
    filename: str = Field(description="Filename of image. Leave empty if not available.")


class ImageURLExtractorOutput(BaseModel):
    images: list[Image] = Field(description="List of images found in the user request.")


class HumanApprovalInput(TypedDict):
    """
    Provide the input for the human approval analysis.
    """

    messages: list[AnyMessage]


class HumanApprovalEvaluation(BaseModel):
    """
    Provide the result of the human approval analysis.
    """

    is_unambiguous_approval: bool = Field(description="Whether the response is an unambiguous approval.")
    approval_phrases: list[str] = Field(description="The phrases that indicate an unambiguous approval.")
    comments: str = Field(description="Additional comments or context regarding the approval.")
    feedback: str = Field(
        description=dedent(
            """\
            Use the same language as the user approval feedback.

            Examples (don't use these exact phrases, just use the same meaning):
            - Thanks for the approval, I'll apply the plan straight away.
            - I can't proceed until a clear approval of the presented plan. Please reply with a clear approval to proceed, or change issue details if the plan doesn't match your expectations.
            """  # noqa: E501
        )
    )


class AskForClarification(BaseModel):
    # Need to add manually `additionalProperties=False` to allow use the schema  as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    questions: str = Field(
        description=dedent(
            """\
            Targeted questions in the same language as the user's request. No chit-chat. Ground them in the codebase and inspection results; use markdown formatting for `variables`, `files`, `directories`, `dependencies` as needed.
            """  # noqa: E501
        )
    )


AskForClarification.__doc__ = FINALIZE_WITH_TARGETED_QUESTIONS_DESCRIPTION


class ChangeInstructions(BaseModel):
    """
    One atomic piece of work a developer can tackle independently.
    If several edits are tightly coupled, group them in the same object and reference the shared `file_path`.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema  as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    relevant_files: list[str] = Field(
        description=dedent(
            """\
            Every file path a developer should open to implement this change (implementation, helpers, tests, docs, configs...). Include ALL files that provide necessary context.
            """  # noqa: E501
        )
    )
    file_path: str = Field(
        description=dedent(
            """\
            Primary file to be modified. Use an empty string ("") if the instruction is repository-wide (e.g., 'add CI workflow'). Otherwise use the canonical path relative to repo root.
            """  # noqa: E501
        )
    )
    details: str = Field(
        description=dedent(
            """\
            Clear, human-readable instructions covering the required change: affected symbols/APIs, algorithms, naming conventions, error handling, edge cases, test approach, performance notes, shell commands to run, etc.
             - **Do NOT** write or paste a full diff / complete implementation you have invented;
             - You **may** embed short illustrative snippets **or** verbatim user-supplied code **only if it is syntactically correct**. If the user's snippet contains errors, describe or reference it in prose instead of pasting the faulty code;
             - Use the safe format: fenced with tildes `~~~language` … `~~~` for markdown code blocks;
             - Use markdown formatting (e.g., for `variables`, `files`, `directories`, `dependencies`) as needed.
            """  # noqa: E501
        )
    )


class Plan(BaseModel):
    # Need to add manually `additionalProperties=False` to allow use the schema  as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    changes: list[ChangeInstructions] = Field(
        description=(
            "List of ChangeInstructions in the order they should be executed. "
            "Group adjacent items when they affect the same file."
        ),
        min_length=1,
    )


Plan.__doc__ = FINALIZE_WITH_PLAN_DESCRIPTION


class FinalizeWithPlanOrTargetedQuestions(BaseModel):
    """
    FINALIZER — The self-contained plan that satisfies the user's request or targeted question(s) to ask the user for clarification.
    """  # noqa: E501

    model_config = ConfigDict(title="finalize_with_plan_or_targeted_questions")

    action: Plan | AskForClarification = Field(description="The plan or the targeted question(s).")
