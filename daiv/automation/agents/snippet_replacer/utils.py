import difflib
import re


def find_original_snippet(snippet: str, file_contents: str, threshold=0.8, initial_line_threshold=0.9) -> list[str]:
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
        list[str]: A list of original snippets from the file.
    """
    if snippet.strip() == "":
        return []

    snippet_lines = [line for line in snippet.split("\n") if line.strip()]
    file_lines = file_contents.split("\n")

    # Find the first non-empty line in the snippet
    first_snippet_line = next((line for line in snippet_lines if line.strip()), "")

    all_matches = []

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
                all_matches.append("\n".join(file_lines[start_index:file_index]))

    return all_matches


def compute_similarity(text1: str, text2: str, ignore_whitespace=True) -> float:
    """
    This function computes the similarity between two pieces of text using the difflib.SequenceMatcher class.

    difflib.SequenceMatcher uses the Ratcliff/Obershelp algorithm: it computes the doubled number of matching
    characters divided by the total number of characters in the two strings.

    Args:
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
