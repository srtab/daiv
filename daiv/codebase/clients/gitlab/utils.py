import re


def replace_section_start_and_end_markers(log: str) -> str:
    """
    Replace section start and end markers with >>> and <<<.

    Args:
        log: Raw log content with section start and end markers

    Returns:
        Log content with section start and end markers replaced with >>> and <<<
    """
    content = re.sub(r"\x1B\[[0-9;]*[a-zA-Z]section_start:[0-9]*:\s*", r">>> ", log)
    content = re.sub(r"\x1B\[[0-9;]*[a-zA-Z]section_end:[0-9]*:\s*", r"<<< ", content)
    content = re.sub(r"section_end:[0-9]*:\s*", r"<<< ", content)

    return content
