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

    plan_questions: list[str]
    """
    The questions to be answered by the human to clarify it's intent.
    """
