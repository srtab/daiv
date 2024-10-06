import difflib
import re
from decimal import Decimal

from langsmith.client import Client
from langsmith.utils import get_tracer_project

from automation.agents.models import Usage


def find_original_snippet(snippet: str, file_contents: str, threshold=0.8, initial_line_threshold=0.9) -> str | None:
    """
    This function finds the original snippet of code in a file given a snippet and the file contents.

    The function first searches for a line in the file that matches the first non-empty line of the snippet
    with a similarity above the initial_line_threshold. It then continues from that point to match the
    rest of the snippet, handling ellipsis cases and using the compute_similarity function to compare
    the accumulated snippet with the file contents.

    Args:
        snippet (str): The snippet of code to find in the file.
        file_contents (str): The contents of the file to search in.
        threshold (float): The similarity threshold for matching the snippet.
        initial_line_threshold (float): The similarity threshold for matching the initial line of the snippet
                                        with a line in the file.

    Returns:
        tuple[str, int, int] | None: A tuple containing the original snippet from the file, start index, and end index,
                                     or None if the snippet could not be found.
    """
    if snippet.strip() == "":
        return None

    snippet_lines = [line for line in snippet.split("\n") if line.strip()]
    file_lines = file_contents.split("\n")

    # Find the first non-empty line in the snippet
    first_snippet_line = next((line for line in snippet_lines if line.strip()), "")

    # Search for a matching initial line in the file
    for start_index, file_line in enumerate(file_lines):
        if compute_similarity(first_snippet_line, file_line) >= initial_line_threshold:
            accumulated_snippet = ""
            snippet_index = 0
            file_index = start_index

            while snippet_index < len(snippet_lines) and file_index < len(file_lines):
                file_line = file_lines[file_index].strip()

                if not file_line:
                    file_index += 1
                    continue

                accumulated_snippet += file_line + "\n"
                similarity = compute_similarity("\n".join(snippet_lines[: snippet_index + 1]), accumulated_snippet)

                if similarity >= threshold:
                    snippet_index += 1

                file_index += 1

            if snippet_index == len(snippet_lines):
                # All lines in the snippet have been matched
                return "\n".join(file_lines[start_index:file_index])

    return None


def compute_similarity(text1: str, text2: str, ignore_whitespace=True) -> float:
    """
    This function computes the similarity between two pieces of text using the difflib.SequenceMatcher class.

    difflib.SequenceMatcher uses the Ratcliff/Obershelp algorithm: it computes the doubled number of matching
    characters divided by the total number of characters in the two strings.

    Parameters:
    text1 (str): The first piece of text.
    text2 (str): The second piece of text.
    ignore_whitespace (bool): If True, ignores whitespace when comparing the two pieces of text.

    Returns:
    float: The similarity ratio between the two pieces of text.
    """
    if ignore_whitespace:
        text1 = re.sub(r"\s+", "", text1)
        text2 = re.sub(r"\s+", "", text2)

    return difflib.SequenceMatcher(None, text1, text2).ratio()


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
