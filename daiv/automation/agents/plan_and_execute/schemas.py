from textwrap import dedent

from langgraph.graph import MessagesState
from langgraph.prebuilt.chat_agent_executor import AgentState
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from automation.agents.schemas import Task


class HumanApproval(BaseModel):
    """
    Provide the result of the human approval analysis.
    """

    is_unambiguous_approval: bool = Field(description="Whether the response is an unambiguous approval.")
    approval_phrases: list[str] = Field(description="The phrases that indicate an unambiguous approval.")
    comments: str = Field(description="Additional comments or context regarding the approval.")
    feedback: str = Field(
        description=dedent(
            """\
            Human friendly feedback to the user about the approval.

            Examples:
            - Thanks for the approval, I'll apply the plan straight away.
            - I can't proceed until a clear approval of the presented plan. Please reply with a clear approval to proceed, or change issue details if the plan doesn't match your expectations.
            """  # noqa: E501
        )
    )


class PlanAndExecuteConfig(TypedDict):
    """
    The configuration for the plan and execute agent.
    """

    source_repo_id: str
    """
    The ID of the source repository.
    """

    source_ref: str
    """
    The reference of the source repository.
    """


class PlanAndExecuteState(MessagesState):
    """
    The state of the overall plan and execute agent.
    """

    plan_questions: list[str]
    """
    The questions to be answered by the human to clarify it's intent.
    """

    plan_goal: str
    """
    The goal of the tasks to be executed.
    """

    plan_tasks: list[Task]
    """
    The tasks to be executed by the agent in order to complete the goal.
    """

    plan_approval_response: str
    """
    The response to be provided to the human when the plan approval is ambiguous.
    """


class PlanState(AgentState):
    """
    The state of the plan agent.
    """


class ExecuteState(AgentState):
    """
    The state of the execute plan agent.
    """

    plan_goal: str
    """
    The goal of the tasks to be executed.
    """

    plan_tasks: list[Task]
    """
    The tasks to be executed by the agent in order to complete the goal.
    """
