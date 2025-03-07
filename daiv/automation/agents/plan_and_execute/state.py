from langgraph.graph import MessagesState
from langgraph.prebuilt.chat_agent_executor import AgentState
from typing_extensions import TypedDict

from .schemas import Task


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

    current_date_time: str
    """
    The current date and time.
    """
