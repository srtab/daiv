from langgraph.graph import MessagesState

from codebase.base import Note


class InputState(MessagesState):
    notes: list[Note]
    diff: str


class OverallState(InputState):
    pass
