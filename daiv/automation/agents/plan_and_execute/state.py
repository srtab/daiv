from langgraph.graph import MessagesState
from langgraph.prebuilt.chat_agent_executor import AgentState
from typing_extensions import TypedDict

from .schemas import ChangeInstructions


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

    plan_questions: str
    """
    The questions to be answered by the human to clarify it's intent.
    """

    plan_tasks: list[ChangeInstructions]
    """
    The code changes to be applied to the codebase.
    """


class ExecuteState(AgentState):
    """
    The state of the execute plan agent.
    """

    plan_tasks: list[ChangeInstructions]
    """
    The code changes to be applied to the codebase.
    """

    relevant_files: list[str]
    """
    The files that are relevant to the code changes.
    """
