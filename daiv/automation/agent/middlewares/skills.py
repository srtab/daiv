from __future__ import annotations

import logging
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NotRequired, override

from deepagents.middleware.skills import SkillMetadata, SkillsState, SkillsStateUpdate
from deepagents.middleware.skills import SkillsMiddleware as DeepAgentsSkillsMiddleware
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import PrivateStateAttr
from langchain.tools import ToolRuntime, tool  # noqa: TC002
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime  # noqa: TC002
from langgraph.types import Command
from skills.constants import SKILLS_CACHE_PATH, SKILLS_PATH
from skills.services import global_skill_files, record_invocation

from automation.agent.constants import SKILLS_TOOL_NAME
from automation.agent.context import RuntimeCtx  # noqa: TC001
from automation.agent.middlewares.file_system import WRITE_TOOL_NAMES
from automation.agent.synthetic import is_person_message
from automation.agent.utils import extract_body_from_frontmatter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from langchain.agents.middleware.types import ToolCallRequest
    from langchain_core.runnables import RunnableConfig
    from langchain_core.tools import BaseTool
    from plugins.runtime import PluginSkillRef


logger = logging.getLogger("daiv.tools")

SKILL_ARGUMENTS_PLACEHOLDER = "$ARGUMENTS"
SKILL_MODE_READ_ONLY = "read-only"
PLUGIN_ROOT_PLACEHOLDER = "${CLAUDE_PLUGIN_ROOT}"


def _keep_read_only(old: str | None, new: str | None) -> str | None:
    """Read-only mode lasts until cleared (``None``): another skill's mode, even one set in the same turn, keeps it."""
    return old if old == SKILL_MODE_READ_ONLY and new is not None else new


class DAIVSkillsState(SkillsState):
    """Extended skills state that tracks the active skill mode."""

    active_skill_mode: NotRequired[Annotated[str | None, PrivateStateAttr, _keep_read_only]]


SKILLS_TOOL_DESCRIPTION = """Execute a skill within the main conversation.

Usage notes:
  - Use this tool with the skill name and optional arguments
  - If the skill does not exist, the tool will return an error.
  - Only use skills listed in <available_skills>.

Examples:
  - `skill: "pdf"` - invoke the pdf skill
  - `skill: "code-review", skill_args: ["my-branch"]` - invoke with arguments
"""

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
- When a skill in <available_skills> matches the request, call the `{SKILLS_TOOL_NAME}` tool before other work on it. Naming a skill in text without calling the tool does nothing.
- Only use skills listed in <available_skills> below, but creation is possible
- Do not invoke a skill that is already running.

**Skill assets:** Each skill lives at `<location>/<skill-name>/` under one of the locations below. The bash working directory is the repository checkout, **not** the skill's root, so a SKILL.md reference to `scripts/foo.py` is `<location>/<skill-name>/scripts/foo.py` (per-repo skills resolve under their `.agents/skills` / `.claude/skills` / `.cursor/skills` source root, not under the bash CWD). Invoke by absolute path; do not probe the bash CWD for skill assets.

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
    Middleware that loads skill metadata and, with ``copy_global_skills``, copies builtin and custom global skills
    into the ``/workspace/skills`` cache so they are available even when the project skills directory is not set up.
    A sandbox's seed provisions them itself, so its workspace turns the copy off and hands over what the seed left out
    as ``provisioning_errors``, reported like the copy's own.
    """

    state_schema = DAIVSkillsState
    # DAIV never sets ``pinned_skills``, so the inherited hook is a no-op that costs a superstep every turn.
    before_model = AgentMiddleware.before_model
    abefore_model = AgentMiddleware.abefore_model

    def __init__(
        self,
        *args,
        copy_global_skills: bool = True,
        provisioning_errors: Sequence[str] = (),
        plugin_skills: Mapping[str, PluginSkillRef] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.system_prompt_template = SKILLS_SYSTEM_PROMPT
        self.tools = [self._skill_tool_generator()]
        self._copies_global_skills = copy_global_skills
        self._provisioning_errors = tuple(provisioning_errors)
        self._plugin_skills = dict(plugin_skills or {})

    async def abefore_agent(
        self, state: DAIVSkillsState, runtime: Runtime[RuntimeCtx], config: RunnableConfig
    ) -> SkillsStateUpdate | dict | None:
        """
        Load skill metadata and, with ``copy_global_skills``, materialize global skills into the
        ``/workspace/skills`` cache; discovery reads the bound backend via ``super().abefore_agent``.

        ``skills_load_errors`` accumulate across turns — the materialize step (``_copy_global_skills``)
        re-runs every turn and reports any newly-seen errors, which are unioned with prior ones here —
        and are never cleared mid-session, so a misconfig fixed mid-session keeps surfacing in
        ``<skill_load_warnings>`` until restart.
        """
        clear_skill_mode = state.get("active_skill_mode") is not None and self._has_user_followup(state["messages"])
        if clear_skill_mode:
            logger.info("[%s] Clearing active skill mode '%s' on user follow-up", self.name, state["active_skill_mode"])

        # Every turn, not only when ``skills_metadata`` is unset: the ``SKILLS_PATH`` cache is per-container but
        # ``skills_metadata`` persists in the checkpoint, so a fresh-worker resume would hit ``file_not_found``.
        local_load_errors = list(self._provisioning_errors)
        if self._copies_global_skills:
            local_load_errors += await self._copy_global_skills()

        skills_update = await super().abefore_agent(state, runtime, config)

        # ``_copy_global_skills`` runs every turn now, so a SKILL.md load error can first arise on
        # a fresh-worker resume — a turn where ``super().abefore_agent`` returns ``None`` because
        # ``skills_metadata`` is already in state. The old ``skills_update is not None`` guard
        # dropped those. ``skills_load_errors`` has no reducer, so writes replace (not append) — we
        # emit the full union of already-known and freshly-collected errors ourselves, and only when
        # it actually changes — keeping ``<skill_load_warnings>`` stable across turns instead of churning.
        if local_load_errors:
            from_super = skills_update.get("skills_load_errors", []) if skills_update else []
            baseline = list(dict.fromkeys([*state.get("skills_load_errors", []), *from_super]))
            merged = list(dict.fromkeys([*baseline, *local_load_errors]))
            if merged != baseline:
                skills_update = {**(skills_update or {}), "skills_load_errors": merged}

        if clear_skill_mode:
            return {**(skills_update or {}), "active_skill_mode": None}

        return skills_update

    async def _copy_global_skills(self) -> list[str]:
        """
        Materialize the global skills (``skills.services.global_skill_files``) into the virtual ``SKILLS_PATH``,
        sibling to the agent's working directory.

        Returns source-level load errors so callers can surface them via ``skills_load_errors``.
        """
        files_to_upload: list[tuple[str, bytes]] = []
        skills = global_skill_files()
        for path in skills.files:
            # ``SKILLS_PATH`` is virtual; its host twin under ``SKILLS_CACHE_PATH`` makes re-uploads a no-op.
            if (SKILLS_CACHE_PATH / path).exists():
                continue
            if (content := skills.read(path)) is not None:
                files_to_upload.append((f"{SKILLS_PATH}/{path}", content))

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

        return skills.errors

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
        if request.tool_call["name"] in WRITE_TOOL_NAMES and (
            request.state.get("active_skill_mode") == SKILL_MODE_READ_ONLY
            or self._calls_read_only_skill_this_turn(request.state)
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
    def _calls_read_only_skill_this_turn(state: dict) -> bool:
        """Whether the turn being executed also calls a read-only skill.

        The skill tool's mode update lands only after every call of the turn has run, so a write
        called beside it still sees the previous ``active_skill_mode``.
        """
        last_ai = next((m for m in reversed(state.get("messages", [])) if isinstance(m, AIMessage)), None)
        if last_ai is None:
            return False

        read_only_skills = {
            skill["name"]
            for skill in state.get("skills_metadata", [])
            if skill.get("metadata", {}).get("mode") == SKILL_MODE_READ_ONLY
        }
        return any(
            call["name"] == SKILLS_TOOL_NAME and call["args"].get("skill") in read_only_skills
            for call in last_ai.tool_calls
        )

    @staticmethod
    def _has_user_followup(messages: list[AnyMessage]) -> bool:
        """Check if the user has sent a follow-up message after the agent replied while a skill was active.

        The pattern we look for (walking backwards from the end):
        1. The latest message is a HumanMessage (user follow-up)
        2. Before it, past any synthetic messages, there's an AIMessage (agent's plan/response) rather than
           another message a person wrote
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
            if is_person_message(msg):
                # Hit another human message before finding an AI message — no agent response yet
                break

        return False

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
            plugin_ref: PluginSkillRef | None = None
            if ":" in skill:
                plugin_ref = self._plugin_skills.get(skill)
                if plugin_ref is None:
                    return ToolMessage(
                        content=f"Skill '{skill}' is not available in this turn.",
                        tool_call_id=runtime.tool_call_id,
                        status="error",
                    )
                loaded_skill = {"name": skill, "path": plugin_ref.path}
            else:
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

            if plugin_ref is not None:
                body = body.replace(PLUGIN_ROOT_PLACEHOLDER, plugin_ref.plugin_root)

            # Skill names can collide between the global tree and per-repo .agents/skills/,
            # so inject the resolved root for relative references inside SKILL.md.
            skill_root = str(Path(loaded_skill["path"]).parent)
            body = (
                f"<skill_root>{skill_root}</skill_root>\n"
                f"Relative paths in this skill resolve under the skill root above.\n\n{body}"
            )

            skill_mode = loaded_skill.get("metadata", {}).get("mode")
            update: dict = {"messages": [ToolMessage(content=body, tool_call_id=runtime.tool_call_id)]}
            if skill_mode:
                update["active_skill_mode"] = skill_mode

            await record_invocation(name=skill, skill_path=loaded_skill["path"], runtime=runtime)

            return Command(update=update)

        return tool(SKILLS_TOOL_NAME, description=SKILLS_TOOL_DESCRIPTION)(skill_tool)
