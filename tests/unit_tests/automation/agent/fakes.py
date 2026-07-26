"""Shared test doubles for the agent test suite."""

from langchain_core.language_models import GenericFakeChatModel


class FakeToolModel(GenericFakeChatModel):
    """Scripted chat model for tests that drive a real compiled agent graph.

    `GenericFakeChatModel` doesn't implement `bind_tools`; returning `self` keeps the
    scripted message iterator on the model instance `create_agent` actually invokes.

    Scripted messages are consumed one per model call, so "the graph stopped calling the
    model" is observable as a message the run never reached.
    """

    def bind_tools(self, tools, **kwargs):
        return self
