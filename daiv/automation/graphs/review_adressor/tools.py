from langchain_core.tools import tool

from .schemas import (
    ACT_RESPONSE_TOOL_NAME,
    ActExecuterResponse,
    ActPlannerResponse,
    AskForClarification,
    Plan,
    Response,
)


@tool(ACT_RESPONSE_TOOL_NAME, args_schema=ActPlannerResponse, infer_schema=False)
def act_planner_response_tool(action: Response | Plan | AskForClarification):
    """
    Use this tool to respond to the reviewer with the proper action to take.

    Args:
        action (Response | Plan | AskForClarification): Next action to be taken.

    Returns:
        dict: The response to the reviewer.
    """
    return ActPlannerResponse(action=action)


@tool(ACT_RESPONSE_TOOL_NAME, args_schema=ActExecuterResponse, infer_schema=False)
def act_executer_response_tool(action: Response | AskForClarification):
    """
    Use this tool to respond to the reviewer with the proper action.

    Args:
        action (Response | Plan | AskForClarification): Next action to be taken.

    Returns:
        dict: The response to the reviewer.
    """
    return ActExecuterResponse(action=action)
