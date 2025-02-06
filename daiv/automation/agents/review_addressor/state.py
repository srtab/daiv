from langgraph.graph import MessagesState


class OverallState(MessagesState):
    diff: str
    show_diff_hunk_to_executor: bool
    request_for_changes: bool
