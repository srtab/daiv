import logging
from typing import TYPE_CHECKING

from langgraph.store.memory import InMemoryStore

from automation.agent.publishers import GitChangePublisher
from codebase.clients import RepoClient
from codebase.utils import GitManager

if TYPE_CHECKING:
    from langchain.agents import CompiledAgent
    from langchain_core.runnables import RunnableConfig

    from codebase.context import RuntimeCtx

logger = logging.getLogger("daiv.managers")


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
        self.git_manager = GitManager(self.ctx.gitrepo)

    async def _recover_draft(
        self, agent: CompiledAgent, config: RunnableConfig, *, entity_label: str, entity_id: int | str
    ) -> bool:
        """
        Attempt to publish a draft MR from the agent's persisted state after an unexpected error.

        Returns:
            Whether a draft merge request was successfully published.
        """
        try:
            snapshot = await agent.aget_state(config=config)
            snapshot_mr = snapshot.values.get("merge_request")

            publisher = GitChangePublisher(self.ctx)
            published_mr = await publisher.publish(
                merge_request=snapshot_mr, as_draft=(snapshot_mr is None or snapshot_mr.draft)
            )

            if published_mr:
                await agent.aupdate_state(config=config, values={"merge_request": published_mr})
                return True
        except Exception:
            logger.exception("Recovery failed after agent error for %s %s", entity_label, entity_id)

        return False

    @staticmethod
    async def _read_code_changes(agent: CompiledAgent, config: RunnableConfig) -> dict[str, bool]:
        """
        Read the ``code_changes`` flag from the agent's persisted state.

        ``code_changes`` is a PrivateStateAttr, so it's omitted from ainvoke output.
        We read it from the persisted checkpoint instead.
        """
        snapshot = await agent.aget_state(config=config)
        return {"code_changes": bool(snapshot.values.get("code_changes"))}
