from __future__ import annotations

import logging
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NotRequired, override

from deepagents.middleware.skills import SkillMetadata, SkillsState, SkillsStateUpdate
from deepagents.middleware.skills import SkillsMiddleware as DeepAgentsSkillsMiddleware
from langchain.agents.middleware import hook_config
from langchain.agents.middleware.types import PrivateStateAttr
from langchain.tools import ToolRuntime, tool  # noqa: TC002
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime  # noqa: TC002
from langgraph.types import Command

from automation.agent.conf import settings as agent_settings
from automation.agent.constants import BUILTIN_SKILLS_PATH, GLOBAL_SKILLS_PATH, SKILLS_CACHE_PATH
from automation.agent.middlewares.file_system import WRITE_TOOL_NAMES
from automation.agent.utils import extract_body_from_frontmatter, extract_text_content
from codebase.context import RuntimeCtx  # noqa: TC001
from slash_commands.parser import SlashCommandCommand, parse_slash_command
from slash_commands.registry import slash_command_registry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from deepagents.graph import SubAgent
    from deepagents.middleware.subagents import CompiledSubAgent
    from langchain.agents.middleware.types import ToolCallRequest
    from langchain_core.runnables import RunnableConfig
    from langchain_core.tools import BaseTool


logger = logging.getLogger("daiv.tools")

SKILL_ARGUMENTS_PLACEHOLDER = "$ARGUMENTS"
SKILL_MODE_READ_ONLY = "read-only"


class DAIVSkillsState(SkillsState):
    """Extended skills state that tracks the active skill mode."""

    active_skill_mode: NotRequired[Annotated[str | None, PrivateStateAttr]]


SKILLS_TOOL_NAME = "skill"
SKILLS_TOOL_DESCRIPTION = """Execute a skill within the main conversation.

Usage notes:
  - Use this tool with the skill name and optional arguments
  - If the skill does not exist, the tool will return an error.
  - Only use skills listed in <available_skills>.
  - CRITICAL: NEVER call this tool in parallel with other tools. The skill tool MUST be the ONLY tool call in any assistant turn that uses it. Calling skill alongside other tools will cause an API error.

Examples:
  - `skill: "pdf"` - invoke the pdf skill
  - `skill: "code-review", skill_args: ["my-branch"]` - invoke with arguments
"""  # noqa: E501

SKILLS_SYSTEM_PROMPT = f"""\
## Skills

**When to Use Skills:**
- When users ask you to perform tasks, check if any of the available skills below can help complete the task more effectively. Skills provide specialized capabilities and domain knowledge.
- When users ask you to run a "slash command" or reference "/" (e.g., "/security-audit", "/code-review"), they are referring to a skill. Use the `{SKILLS_TOOL_NAME}` tool to invoke the corresponding skill.

<example>
  User: "run /code-review"
  Assistant: [Calls `{SKILLS_TOOL_NAME}` tool with skill name: "code-review"]
  ...
</example>
<example>
  User: "Plan to fix issue #42."
  Assistant: [Calls `{SKILLS_TOOL_NAME}` tool with skill name: "plan"]
  ...
</example>

**Important:**
- When a skill is relevant, you must invoke the `{SKILLS_TOOL_NAME}` tool IMMEDIATELY as your first action.
- NEVER just announce or mention a skill in your text response without actually calling the `{SKILLS_TOOL_NAME}` tool.
- This is a BLOCKING REQUIREMENT: invoke the relevant `{SKILLS_TOOL_NAME}` tool BEFORE generating any other response about the task.
- CRITICAL: The `{SKILLS_TOOL_NAME}` tool MUST be the ONLY tool call in its assistant turn. NEVER call `{SKILLS_TOOL_NAME}` in parallel with other tools (e.g., do NOT call `{SKILLS_TOOL_NAME}` and `gitlab` at the same time). Other tools can be called in subsequent turns after the skill has been processed.
- Only use skills listed in <available_skills> below, but creation is possible
- Do not invoke a skill that is already running.

{{skills_locations}}{{skills_load_warnings}}

{{skills_list}}"""  # noqa: E501


AVAILABLE_SKILLS_TEMPLATE = PromptTemplate.from_template(
    """<available_skills>
  {{#skills_list}}
  <skill>
    <name>{{name}}</name>
    <description>{{description}}</description>
  </skill>
  {{/skills_list}}
</available_skills>""",
    template_format="mustache",
)


class SkillsMiddleware(DeepAgentsSkillsMiddleware):
    """
    Middleware to apply builtin slash commands early in the conversation and copy builtin skills to the project skills
    directory to make them available to the agent even if the project skills directory is not set up.
    """

    state_schema = DAIVSkillsState

    def __init__(self, *args, subagents: Sequence[SubAgent | CompiledSubAgent] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.system_prompt_template = SKILLS_SYSTEM_PROMPT
        self.tools = [self._skill_tool_generator()]
        self.subagents = subagents or []

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(
        self, state: DAIVSkillsState, runtime: Runtime[RuntimeCtx], config: RunnableConfig
    ) -> SkillsStateUpdate | dict | None:
        """
        Apply builtin slash commands early in the conversation and copy builtin skills to the project skills directory
        to make them available to the agent.

        ``skills_load_errors`` follow upstream's load-once contract: they are populated on the first turn
        (when ``skills_metadata`` is not yet in state) and preserved across subsequent turns via LangGraph's
        last-write-wins merge. Errors are not re-validated mid-session — a transient misconfig that's fixed
        after the first turn will continue surfacing in ``<skill_load_warnings>`` until the session restarts.
        """
        # Clear the active skill mode when the user sends a follow-up message. This allows the agent to transition
        # from plan mode (read-only) to implementation mode (full tool access) when the user says "proceed" or similar.
        clear_skill_mode = state.get("active_skill_mode") is not None and self._has_user_followup(state["messages"])
        if clear_skill_mode:
            logger.info("[%s] Clearing active skill mode '%s' on user follow-up", self.name, state["active_skill_mode"])

        # Materialize before super() so the `skill` tool can resolve files on disk,
        # not just metadata; otherwise the agent gets a not_found at invocation time.
        local_load_errors = await self._copy_global_skills()

        skills_update = await super().abefore_agent(state, runtime, config)

        # Merge daiv-side load errors (custom-skills misconfig, unreadable SKILL.md) into the
        # upstream `skills_load_errors` channel so they render in `<skill_load_warnings>`.
        # Only attach to `skills_update` on the first turn; once skills_metadata is in state,
        # upstream returns None and LangGraph keeps the prior turn's errors via merge.
        if local_load_errors and skills_update is not None:
            skills_update.setdefault("skills_load_errors", []).extend(local_load_errors)

        # If the super method returns None, it means that the skills metadata was already captured and registered in
        # the state.
        skills_metadata = skills_update["skills_metadata"] if skills_update else state["skills_metadata"]

        builtin_slash_commands = None
        if runtime.context.config.slash_commands.enabled:
            builtin_slash_commands = await self._apply_builtin_slash_commands(
                state["messages"], runtime.context, skills_metadata
            )

        if builtin_slash_commands:
            if skills_update is not None and "skills_load_errors" in skills_update:
                builtin_slash_commands["skills_load_errors"] = skills_update["skills_load_errors"]
            if clear_skill_mode:
                builtin_slash_commands["active_skill_mode"] = None
            return builtin_slash_commands

        # The spread is load-bearing on the first turn: it copies `skills_metadata` AND
        # `skills_load_errors` from skills_update into the return so they reach the next
        # `wrap_model_call`. On subsequent turns skills_update is None and LangGraph's
        # last-write-wins merge preserves both keys from state. Tests at
        # `test_abefore_agent_forwards_skills_load_errors_through_*_branch` pin this contract.
        if clear_skill_mode:
            return {**(skills_update or {}), "active_skill_mode": None}

        return skills_update

    async def _copy_global_skills(self) -> list[str]:
        """
        Materialize builtin and custom global skills into the virtual ``GLOBAL_SKILLS_PATH``,
        sibling to the agent's working directory.

        Custom global skills override builtins with the same name on the first cache-population
        pass (later writes in ``files_to_upload`` win); once ``SKILLS_CACHE_PATH`` is warm both
        writes become no-ops, so override semantics only apply at first materialization.

        Returns source-level load errors (custom-skills-dir misconfig, OSError while walking,
        unreadable ``SKILL.md`` files) so callers can surface them via ``skills_load_errors``.
        """
        files_to_upload: list[tuple[str, bytes]] = []
        errors: list[str] = []
        skills_path = Path(GLOBAL_SKILLS_PATH)

        self._collect_skill_files(BUILTIN_SKILLS_PATH, skills_path, files_to_upload, errors)

        custom_skills_path = agent_settings.CUSTOM_SKILLS_PATH
        if custom_skills_path is not None and custom_skills_path.is_dir():
            try:
                self._collect_skill_files(custom_skills_path, skills_path, files_to_upload, errors)
            except OSError as exc:
                logger.exception("Failed to read custom global skills from '%s'", custom_skills_path)
                errors.append(f"Cannot load custom global skills from '{custom_skills_path}': {exc}")
        elif custom_skills_path is not None:
            msg = f"Custom global skills path '{custom_skills_path}' does not exist or is not a directory"
            logger.warning(msg)
            errors.append(msg)

        responses = await self._backend.aupload_files(files_to_upload)
        failures = [
            (dest, resp.error) for (dest, _), resp in zip(files_to_upload, responses, strict=True) if resp.error
        ]
        if failures:
            for dest, err in failures:
                logger.error("Skill upload failed: dest=%s error=%s", dest, err)
            first_dest, first_err = failures[0]
            extra = f"; first failure at '{first_dest}': {first_err}"
            raise RuntimeError(f"Failed to upload {len(failures)} skill file(s){extra}")

        return errors

    @staticmethod
    def _collect_skill_files(
        source_root: Path, project_skills_path: Path, files_to_upload: list[tuple[str, bytes]], errors: list[str]
    ) -> None:
        """
        Walk skill directories under ``source_root`` and append files to ``files_to_upload``.

        Records ``SKILL.md`` read failures into ``errors`` so the skill is not silently dropped
        from the agent's view. Helper-file read failures are logged at warning level and skipped
        without escalation (a skill with a usable ``SKILL.md`` but a broken helper is still
        partially usable).
        """
        for skill_dir in source_root.iterdir():
            if not skill_dir.is_dir() or skill_dir.name == "__pycache__":
                continue

            for root, dirs, files in skill_dir.walk():
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for file in files:
                    source_path = Path(root) / Path(file)
                    if source_path.suffix == ".pyc":
                        continue
                    rel = source_path.relative_to(source_root)
                    # Real existence check on the disk-backed skills cache so per-turn
                    # uploads become a no-op once the cache is populated. ``dest_path``
                    # is a virtual path under ``GLOBAL_SKILLS_PATH``, which never exists
                    # on the host fs — the disk equivalent is ``SKILLS_CACHE_PATH/rel``.
                    dest_path = project_skills_path / rel
                    if (SKILLS_CACHE_PATH / rel).exists():
                        continue
                    try:
                        files_to_upload.append((str(dest_path), source_path.read_bytes()))
                    except OSError as exc:
                        logger.warning(
                            "Failed to read skill file '%s' (skill='%s'), skipping", source_path, skill_dir.name
                        )
                        if source_path.name == "SKILL.md":
                            errors.append(
                                f"Cannot read SKILL.md for skill '{skill_dir.name}' at '{source_path}': {exc}"
                            )

    @override
    def _format_skills_list(self, skills: list[SkillMetadata]) -> str:
        """
        Format the skills list for the system prompt.

        Args:
            skills: The list of skills.

        Returns:
            The formatted skills list.
        """
        if not skills:
            paths = [f"{source_path}" for source_path in self.sources]
            return f"(No skills available yet. You can create skills in {' or '.join(paths)})"

        return AVAILABLE_SKILLS_TEMPLATE.format(skills_list=skills)

    @override
    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        """Refuse write-tool execution when the active skill declares read-only mode.

        Enforced at the tool layer rather than by stripping tools from the model request,
        so the cached prompt prefix (which includes the tool list) stays stable across
        skill activation and skill exit. Filtering at request-time invalidates the
        Anthropic prompt cache and forces a full prefix re-create on the next call.
        """
        if (
            request.tool_call["name"] in WRITE_TOOL_NAMES
            and request.state.get("active_skill_mode") == SKILL_MODE_READ_ONLY
        ):
            return ToolMessage(
                content=(
                    f"Refused: tool '{request.tool_call['name']}' is unavailable while a read-only skill is active. "
                    "Read-only skills must not modify files. Wait for the user's follow-up before writing."
                ),
                tool_call_id=request.tool_call["id"],
                status="error",
            )
        return await handler(request)

    @staticmethod
    def _has_user_followup(messages: list[AnyMessage]) -> bool:
        """Check if the user has sent a follow-up message after the agent responded to a skill injection.

        The pattern we look for (walking backwards from the end):
        1. The latest message is a HumanMessage (user follow-up)
        2. Before it, there's an AIMessage (agent's plan/response)
        """
        if len(messages) < 2:
            return False

        if not isinstance(messages[-1], HumanMessage):
            return False

        # Walk backwards to find an AIMessage before this HumanMessage
        for i in range(len(messages) - 2, -1, -1):
            msg = messages[i]
            if isinstance(msg, AIMessage):
                return True
            if isinstance(msg, HumanMessage):
                # Hit another human message before finding an AI message — no agent response yet
                break

        return False

    async def _apply_builtin_slash_commands(
        self, messages: list[AnyMessage], context: RuntimeCtx, skills: list[SkillMetadata]
    ) -> SkillsStateUpdate | None:
        """
        Detect and execute builtin slash commands (not project skills) early in the conversation.

        Args:
            messages: The list of messages.
            context: The runtime context.
            skills: The list of skills.

        Returns:
            State update with messages injected, or None if no builtin slash command detected.
        """
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
                available_skills=skills,
                available_subagents=self.subagents,
            )
        except Exception:
            logger.exception("[%s] Failed to execute `%s` slash command", self.name, slash_command.raw)
            return {"messages": [AIMessage(content=f"Failed to execute `{slash_command.raw}`.")], "jump_to": "end"}
        else:
            logger.info("[%s] `%s` slash command completed", self.name, slash_command.raw)
            return {"messages": [AIMessage(content=result)], "jump_to": "end"}

    def _extract_slash_command(self, messages: list[AnyMessage], bot_username: str) -> SlashCommandCommand | None:
        """
        Extract the slash command from the latest message.

        Args:
            messages: The list of messages.
            bot_username: The username of the bot.

        Returns:
            The slash command command if found, otherwise None.
        """
        latest_message = messages[-1]

        if not hasattr(latest_message, "type") or latest_message.type != "human":
            return None

        text_content = extract_text_content(latest_message.content)
        if not text_content or not text_content.strip():
            return None

        return parse_slash_command(text_content, bot_username)

    def _skill_tool_generator(self) -> BaseTool:
        """Generate a skill tool."""

        async def skill_tool(
            skill: Annotated[str, "The skill name. E.g. 'code-review' or 'web-research'"],
            runtime: ToolRuntime[RuntimeCtx, SkillsState],
            skill_args: Annotated[str | None, "Optional arguments to pass to the skill."] = None,
        ) -> str | Command:
            """
            Tool to execute a skill.
            """
            available_skills = runtime.state["skills_metadata"]

            loaded_skill = next(
                (skill_metadata for skill_metadata in available_skills if skill_metadata["name"] == skill), None
            )

            if loaded_skill is None:
                available_skills_names = [skill_metadata["name"] for skill_metadata in available_skills]
                return f"error: Skill '{skill}' not found. Available skills: {', '.join(available_skills_names)}."

            responses = await self._backend.adownload_files([loaded_skill["path"]])
            if responses[0].error:
                return f"error: Failed to launch skill '{skill}': {responses[0].error}."

            body = extract_body_from_frontmatter(responses[0].content.decode("utf-8").strip())

            try:
                # Positional args like $1, $2
                for i, a in enumerate(shlex.split(skill_args or ""), start=1):
                    body = body.replace(f"${i}", a).replace(f"{SKILL_ARGUMENTS_PLACEHOLDER}[{i}]", a)
            except ValueError:
                logger.warning(
                    "[%s] Failed to apply positional arguments, falling back to named arguments",
                    self.name,
                    exc_info=True,
                )

            # Named args, only $ARGUMENTS supported
            if skill_args and (arg_str := skill_args.strip()):
                body = (
                    body.replace(SKILL_ARGUMENTS_PLACEHOLDER, arg_str)
                    if SKILL_ARGUMENTS_PLACEHOLDER in body
                    else f"{body}\n\n{SKILL_ARGUMENTS_PLACEHOLDER}: {arg_str}"
                )

            skill_mode = loaded_skill.get("metadata", {}).get("mode")
            update: dict = {
                "messages": [
                    ToolMessage(content=f"Launching skill '{skill}'...", tool_call_id=runtime.tool_call_id),
                    HumanMessage(content=body),
                ]
            }
            if skill_mode:
                update["active_skill_mode"] = skill_mode

            return Command(update=update)

        return tool(SKILLS_TOOL_NAME, description=SKILLS_TOOL_DESCRIPTION)(skill_tool)
