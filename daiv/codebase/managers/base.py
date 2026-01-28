from typing import TYPE_CHECKING

from langgraph.store.memory import InMemoryStore

from codebase.clients import RepoClient
from codebase.utils import GitManager

if TYPE_CHECKING:
    from codebase.context import RuntimeCtx


class BaseManager:
    """
    Base class for all managers.
    """

    _comment_id: str | None = None
    """ The comment ID where DAIV comments are stored. """

    def __init__(self, *, runtime_ctx: RuntimeCtx):
        self.ctx = runtime_ctx
        self.client = RepoClient.create_instance()
        self.store = InMemoryStore()
        self.git_manager = GitManager(self.ctx.repo)
