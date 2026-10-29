from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AnyMessage, RemoveMessage  # noqa: TC002
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime  # noqa: TC002
from skills.services import list_global_skills

from automation.agent.context import RuntimeCtx  # noqa: TC001
from automation.agent.middlewares.skills import DAIVSkillsState
from automation.agent.synthetic import is_person_message
from automation.agent.utils import extract_text_content, streamed_assistant_message
from slash_commands.parser import SlashCommandCommand, parse_slash_command
from slash_commands.registry import slash_command_registry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from deepagents.graph import SubAgent
    from deepagents.middleware.subagents import CompiledSubAgent
    from langchain_core.messages import BaseMessage
    from langchain_core.runnables import RunnableConfig

    from automation.agent.agent_settings import AgentSettings
    from slash_commands.base import SlashCommand

logger = logging.getLogger("daiv.tools")


class SlashCommandMiddleware(AgentMiddleware):
    """Intercept builtin slash commands before the agent loop.

    Commands that reset/inspect the thread (``/clear``, ``/help``, ``/agents``, ...) short-circuit
    the run. The run executor starts no sandbox for one that does not read the repository
    (:func:`skips_sandbox`), and ``/agents`` gets the subagents the agent was built with. Builtin
    commands need only that static context plus, for ``/help``, the *global* skill list — read from
    disk, so this hook never touches the sandbox backend. Per-repo skills live in the sandbox, so
    ``/help`` cannot list them; the agent's ``<available_skills>`` prompt does once the loop runs.
    """

    # Declares the skills state channels so this middleware may reset ``active_skill_mode`` and ``active_skill_model``
    # when a command wipes the thread (see ``_apply_builtin_slash_commands``); ``SkillsMiddleware`` owns them.
    state_schema = DAIVSkillsState

    def __init__(self, *, subagents: Sequence[SubAgent | CompiledSubAgent] | None = None) -> None:
        super().__init__()
        self.subagents = subagents or []

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(self, state: dict, runtime: Runtime[RuntimeCtx], config: RunnableConfig) -> dict | None:
        return await self._apply_builtin_slash_commands(state["messages"], runtime.context, config)

    async def _apply_builtin_slash_commands(
        self, messages: list[AnyMessage], context: RuntimeCtx, config: RunnableConfig
    ) -> dict | None:
        slash_command = self._extract_slash_command(messages, context.bot_username)
        if not slash_command:
            return None
        command_class = _builtin_command_class(slash_command, context)
        if command_class is None:
            return None

        command = command_class(scope=context.scope, repo_id=context.repository.slug, bot_username=context.bot_username)
        logger.info("[%s] Executing `%s` slash command", self.name, slash_command.raw)

        try:
            result = await command.execute_for_agent(
                args=" ".join(slash_command.args),
                repo_client=context.repo.client,
                issue_iid=context.issue.iid if context.issue else None,
                merge_request_id=context.merge_request.merge_request_id if context.merge_request else None,
                available_skills=list_global_skills(),
                available_subagents=self.subagents,
            )
        except Exception:
            logger.exception("[%s] Failed to execute `%s` slash command", self.name, slash_command.raw)
            # Do NOT honor resets_thread here: if execute_for_agent raised, any checkpointer-side wipe
            # may not have run, so keeping in-memory history avoids diverging from the Redis checkpoint.
            reply = await streamed_assistant_message(f"Failed to execute `{slash_command.raw}`.", config)
            return {"messages": [reply], "jump_to": "end"}
        else:
            logger.info("[%s] `%s` slash command completed", self.name, slash_command.raw)
            # resets_thread commands (e.g. /clear) must drop in-memory history, else the final
            # checkpoint write re-persists every prior message under the same thread_id.
            reset_prefix: list = [RemoveMessage(id=REMOVE_ALL_MESSAGES)] if command.resets_thread else []
            reply = await streamed_assistant_message(result, config)
            update: dict = {"messages": [*reset_prefix, reply], "jump_to": "end"}
            if command.resets_thread:
                # The wipe also clears private state: a read-only skill (e.g. /plan sets
                # active_skill_mode="read-only") must not survive onto the fresh thread, where
                # _has_user_followup (needs surviving history) could never clear it — every write
                # tool would stay refused. SkillsMiddleware's clear-on-followup never runs here
                # (we jump to end), so reset it, and the skill's model, explicitly.
                update["active_skill_mode"] = None
                update["active_skill_model"] = None
            return update

    @staticmethod
    def _extract_slash_command(messages: Sequence[BaseMessage], bot_username: str) -> SlashCommandCommand | None:
        latest_message = messages[-1]
        if not is_person_message(latest_message):
            return None
        text_content = extract_text_content(latest_message.content)
        if not text_content or not text_content.strip():
            return None
        return parse_slash_command(text_content, bot_username)


def skips_sandbox(messages: Sequence[BaseMessage], context: RuntimeCtx, settings: AgentSettings) -> bool:
    """Whether the latest of ``messages`` invokes a builtin slash command that needs no sandbox:
    ``SlashCommandMiddleware``, which ``settings`` enables, answers it without the agent loop, and it does not read the
    repository (``SlashCommand.reads_repository``). The run executor then acquires none."""
    command_class = _invoked_builtin_command(messages, context, settings)
    return command_class is not None and not command_class.reads_repository


def resets_thread(messages: Sequence[BaseMessage], context: RuntimeCtx, settings: AgentSettings) -> bool:
    """Whether the latest of ``messages`` invokes a builtin slash command that resets the thread
    (``SlashCommand.resets_thread``, e.g. ``/clear``), which ``SlashCommandMiddleware``, enabled by ``settings``,
    answers."""
    command_class = _invoked_builtin_command(messages, context, settings)
    return command_class is not None and command_class.resets_thread


def _invoked_builtin_command(
    messages: Sequence[BaseMessage], context: RuntimeCtx, settings: AgentSettings
) -> type[SlashCommand] | None:
    if not settings.features.slash_commands or not messages:
        return None
    slash_command = SlashCommandMiddleware._extract_slash_command(messages, context.bot_username)
    if slash_command is None:
        return None
    return _builtin_command_class(slash_command, context)


def _builtin_command_class(slash_command: SlashCommandCommand, context: RuntimeCtx) -> type[SlashCommand] | None:
    """The one builtin command ``slash_command`` names in ``context``'s scope; ``None`` for none or several (logged)."""
    command_classes = slash_command_registry.get_commands(scope=context.scope, command=slash_command.command)
    if len(command_classes) > 1:
        logger.warning(
            "Multiple `%s` slash commands found for scope '%s': %r",
            slash_command.command,
            context.scope.value,
            [c.command for c in command_classes],
        )
    return command_classes[0] if len(command_classes) == 1 else None
