from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from deepagents.middleware.skills import SkillMetadata, _parse_skill_metadata
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, AnyMessage, RemoveMessage  # noqa: TC002
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime  # noqa: TC002

from automation.agent.conf import settings as agent_settings
from automation.agent.constants import BUILTIN_SKILLS_PATH
from automation.agent.middlewares.skills import DAIVSkillsState
from automation.agent.utils import extract_text_content
from codebase.context import RuntimeCtx  # noqa: TC001
from slash_commands.parser import SlashCommandCommand, parse_slash_command
from slash_commands.registry import slash_command_registry

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from deepagents.graph import SubAgent
    from deepagents.middleware.subagents import CompiledSubAgent
    from langchain_core.runnables import RunnableConfig

logger = logging.getLogger("daiv.tools")


def _load_global_skill_metadata() -> list[SkillMetadata]:
    """Read builtin + custom *global* skill metadata from disk (name/description).

    Used by :class:`SlashCommandMiddleware` for ``/help``, which runs before the sandbox
    session exists — so it cannot read per-repo skills (those live in the sandbox). The
    agent's ``<available_skills>`` system prompt still enumerates everything once the loop
    runs; this is only the ``/help`` listing. Custom skills override builtins of the same
    name (later source wins).
    """
    skills: dict[str, SkillMetadata] = {}
    roots: list[Path] = [BUILTIN_SKILLS_PATH]
    custom = agent_settings.CUSTOM_SKILLS_PATH
    if custom is not None and custom.is_dir():
        roots.append(custom)

    for root in roots:
        try:
            children = sorted(root.iterdir())
        except OSError:
            logger.warning("Could not read global skills root '%s' for /help", root, exc_info=True)
            continue
        for skill_dir in children:
            if not skill_dir.is_dir() or skill_dir.name.startswith(".") or skill_dir.name == "__pycache__":
                continue
            skill_md = skill_dir / "SKILL.md"
            try:
                content = skill_md.read_text(encoding="utf-8")
            except OSError:
                logger.warning("Could not read SKILL.md '%s' for /help; omitting from listing", skill_md, exc_info=True)
                continue
            meta = _parse_skill_metadata(content, str(skill_md), skill_dir.name)
            if meta is not None:
                skills[meta["name"]] = meta
    return list(skills.values())


class SlashCommandMiddleware(AgentMiddleware):
    """Intercept builtin slash commands before the sandbox session starts.

    Runs ahead of ``SandboxMiddleware`` so commands that reset/inspect the thread
    (``/clear``, ``/help``, ``/agents``, ...) short-circuit the run without paying for a
    sandbox session. Builtin commands need only static context (the configured subagents)
    plus, for ``/help``, the builtin + custom *global* skill list — all read from disk, so
    this hook never touches the sandbox backend.
    """

    # Declares the skills state channels so this middleware may reset ``active_skill_mode`` when a
    # command wipes the thread (see ``_apply_builtin_slash_commands``); ``SkillsMiddleware`` owns it.
    state_schema = DAIVSkillsState

    def __init__(self, *, subagents: Sequence[SubAgent | CompiledSubAgent] | None = None) -> None:
        super().__init__()
        self.subagents = subagents or []

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(self, state: dict, runtime: Runtime[RuntimeCtx], config: RunnableConfig) -> dict | None:
        if not runtime.context.config.slash_commands.enabled:
            return None
        return await self._apply_builtin_slash_commands(state["messages"], runtime.context)

    async def _apply_builtin_slash_commands(self, messages: list[AnyMessage], context: RuntimeCtx) -> dict | None:
        slash_command = self._extract_slash_command(messages, context.bot_username)
        if not slash_command:
            return None

        command_classes = slash_command_registry.get_commands(scope=context.scope, command=slash_command.command)
        if not command_classes:
            return None

        if len(command_classes) > 1:
            logger.warning(
                "[%s] Multiple `%s` slash commands found for scope '%s': %r",
                self.name,
                slash_command.command,
                context.scope.value,
                [c.command for c in command_classes],
            )
            return None

        command = command_classes[0](
            scope=context.scope, repo_id=context.repository.slug, bot_username=context.bot_username
        )
        logger.info("[%s] Executing `%s` slash command", self.name, slash_command.raw)

        try:
            result = await command.execute_for_agent(
                args=" ".join(slash_command.args),
                issue_iid=context.issue.iid if context.issue else None,
                merge_request_id=context.merge_request.merge_request_id if context.merge_request else None,
                available_skills=_load_global_skill_metadata(),
                available_subagents=self.subagents,
            )
        except Exception:
            logger.exception("[%s] Failed to execute `%s` slash command", self.name, slash_command.raw)
            # Do NOT honor resets_thread here: if execute_for_agent raised, any checkpointer-side wipe
            # may not have run, so keeping in-memory history avoids diverging from the Redis checkpoint.
            return {"messages": [AIMessage(content=f"Failed to execute `{slash_command.raw}`.")], "jump_to": "end"}
        else:
            logger.info("[%s] `%s` slash command completed", self.name, slash_command.raw)
            # resets_thread commands (e.g. /clear) must drop in-memory history, else the final
            # checkpoint write re-persists every prior message under the same thread_id.
            reset_prefix: list = [RemoveMessage(id=REMOVE_ALL_MESSAGES)] if command.resets_thread else []
            update: dict = {"messages": [*reset_prefix, AIMessage(content=result)], "jump_to": "end"}
            if command.resets_thread:
                # The wipe also clears private state: a read-only skill (e.g. /plan sets
                # active_skill_mode="read-only") must not survive onto the fresh thread, where
                # _has_user_followup (needs surviving history) could never clear it — every write
                # tool would stay refused. SkillsMiddleware's clear-on-followup never runs here
                # (we jump to end), so reset it explicitly.
                update["active_skill_mode"] = None
            return update

    def _extract_slash_command(self, messages: list[AnyMessage], bot_username: str) -> SlashCommandCommand | None:
        latest_message = messages[-1]
        if not hasattr(latest_message, "type") or latest_message.type != "human":
            return None
        text_content = extract_text_content(latest_message.content)
        if not text_content or not text_content.strip():
            return None
        return parse_slash_command(text_content, bot_username)
