import re

#: The ``source`` of a child pipeline, which runs on its parent's ref and commit.
CHILD_PIPELINE_SOURCE = "parent_pipeline"

MERGE_REQUEST_TITLE_MAX_LENGTH = 255

_DRAFT_TITLE_PREFIX = re.compile(r"\s*(?:(?:\[draft\]|\(draft\)|draft:)\s*)+", re.IGNORECASE)


def title_in_draft_state(title: str, *, draft: bool) -> str:
    """
    Return the title that puts a merge request in the given draft state.

    GitLab's REST API has no draft parameter: a merge request is a draft exactly when its title starts
    with ``Draft:``, ``[Draft]`` or ``(Draft)``. The title is cut to GitLab's limit when the prefix is
    added, and returned unchanged when it is nothing but the prefix, as GitLab rejects a blank one.
    """
    if bool(_DRAFT_TITLE_PREFIX.match(title)) == draft:
        return title
    if draft:
        return f"Draft: {title}"[:MERGE_REQUEST_TITLE_MAX_LENGTH]
    return _DRAFT_TITLE_PREFIX.sub("", title, count=1) or title


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
