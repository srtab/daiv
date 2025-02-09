from langgraph.graph import MessagesState


class OverallState(MessagesState):
    """
    The state of the review addressor agent.
    """

    diff: str
    """
    The unified diff of the merge request.
    """

    reply: str
    """
    The reply to show to the reviewer.

    It can be a direct reply to the comment left by the reviewer or questions from the plan and execute node to be
    clarified by the reviewer.
    """

    requested_changes: list[str]
    """
    The summarised requested changes stated by the reviewer.

    This is used to feed the plan and execute node with the requested changes required by the reviewer.
    """
