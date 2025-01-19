from langgraph.graph import MessagesState


class OverallState(MessagesState):
    context: str
