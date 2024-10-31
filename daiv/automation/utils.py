from decimal import Decimal

from langsmith.client import Client
from langsmith.utils import get_tracer_project

from automation.agents import Usage


def total_traced_cost(agent_name: str, filter_by: dict) -> Usage:
    """
    This function calculates the total cost of all traced runs for a given agent with the specified metadata.

    Args:
        agent_name (str): The name of the agent.
        filter_by (dict): The metadata to filter the runs by.

    Returns:
        Usage: The total cost of the traced runs.
    """
    metadata = [f'eq(metadata_key, "{key}")' for key in filter_by]
    for value in filter_by.values():
        if isinstance(value, int):
            metadata.append(f"eq(metadata_value, {value})")
        else:
            metadata.append(f'eq(metadata_value, "{value}")')

    filter_str = f'and(eq(name, "{agent_name}"), {", ".join(metadata)})'

    project_runs = Client().list_runs(
        project_name=get_tracer_project(),
        is_root=True,
        select=["prompt_tokens", "completion_tokens", "total_tokens", "prompt_cost", "completion_cost", "total_cost"],
        filter=filter_str,
    )
    usage = Usage()
    for run in project_runs:
        usage += Usage(
            prompt_tokens=run.prompt_tokens or 0,
            completion_tokens=run.completion_tokens or 0,
            total_tokens=run.total_tokens or 0,
            prompt_cost=run.prompt_cost or Decimal(0.0),
            completion_cost=run.completion_cost or Decimal(0.0),
            total_cost=run.total_cost or Decimal(0.0),
        )
    return usage
