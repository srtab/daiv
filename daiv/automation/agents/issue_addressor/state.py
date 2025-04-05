from langgraph.graph import MessagesState


class OverallState(MessagesState):
    issue_title: str
    """
    The title of the issue.
    """

    issue_description: str
    """
    The description of the issue.
    """

    request_for_changes: bool
    """
    Whether the issue is a request for changes.
    """

    image_templates: list[dict]
    """
    The image templates to be used in the issue addressor.
    """
