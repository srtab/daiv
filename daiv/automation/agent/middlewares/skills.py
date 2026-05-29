from __future__ import annotations

import logging
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NotRequired, override

from deepagents.middleware.skills import SkillMetadata, SkillsState, SkillsStateUpdate
from deepagents.middleware.skills import SkillsMiddleware as DeepAgentsSkillsMiddleware
from langchain.agents.middleware.types import PrivateStateAttr
from langchain.tools import ToolRuntime, tool  # noqa: TC002
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime  # noqa: TC002
from langgraph.types import Command
from skills.services import _record_invocation

from automation.agent.conf import settings as agent_settings
from automation.agent.constants import BUILTIN_SKILLS_PATH, SKILLS_CACHE_PATH, SKILLS_PATH
from automation.agent.middlewares.file_system import WRITE_TOOL_NAMES
from automation.agent.utils import extract_body_from_frontmatter
from codebase.context import RuntimeCtx  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

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
    Middleware that loads skill metadata and, in disk-backed (non-sandbox) mode, copies builtin
    and custom global skills into the ``/workspace/skills`` cache so they are available even when the
    project skills directory is not set up. In sandbox mode global skills are provisioned by the
    sandbox seed (SandboxMiddleware), so no upload happens here.
    """

    state_schema = DAIVSkillsState

    def __init__(self, *args, sandbox_enabled: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.system_prompt_template = SKILLS_SYSTEM_PROMPT
        self.tools = [self._skill_tool_generator()]
        self._sandbox_enabled = sandbox_enabled

    async def abefore_agent(
        self, state: DAIVSkillsState, runtime: Runtime[RuntimeCtx], config: RunnableConfig
    ) -> SkillsStateUpdate | dict | None:
        """
        Load skill metadata and (disk-mode only) materialize global skills into the
        ``/workspace/skills`` cache. In sandbox mode global skills are provisioned by the sandbox
        seed (SandboxMiddleware), so no upload happens here; discovery reads the bound,
        seeded sandbox via ``super().abefore_agent``.

        ``skills_load_errors`` are captured once per session and persist through subsequent
        turns; a misconfig fixed mid-session will keep surfacing until restart.
        """
        clear_skill_mode = state.get("active_skill_mode") is not None and self._has_user_followup(state["messages"])
        if clear_skill_mode:
            logger.info("[%s] Clearing active skill mode '%s' on user follow-up", self.name, state["active_skill_mode"])

        # In disk (non-sandbox) mode, materialize global skills on every turn rather than only when
        # ``skills_metadata`` is unset: the ``SKILLS_PATH`` cache is per-container while
        # ``skills_metadata`` is persisted in the Redis checkpoint, so a turn that resumes on a fresh
        # worker (rolling deploy, scale-up, pod restart) would otherwise hit ``file_not_found`` when
        # the ``skill`` tool downloads ``SKILL.md`` from disk. ``_collect_skill_files`` is idempotent
        # via a per-file existence check, so warm containers only pay an ``iterdir`` + per-file
        # ``stat``. In sandbox mode the sandbox seed (SandboxMiddleware) provisions global skills, so
        # nothing is copied here.
        local_load_errors: list[str] = []
        if not self._sandbox_enabled:
            local_load_errors = await self._copy_global_skills()

        skills_update = await super().abefore_agent(state, runtime, config)

        # ``_copy_global_skills`` runs every turn now, so a SKILL.md load error can first arise on
        # a fresh-worker resume — a turn where ``super().abefore_agent`` returns ``None`` because
        # ``skills_metadata`` is already in state. The old ``skills_update is not None`` guard
        # dropped those. ``skills_load_errors`` has a replace reducer (no append), so we emit the
        # full union of already-known and freshly-collected errors, and only when it actually
        # changes — keeping ``<skill_load_warnings>`` stable across turns instead of churning.
        if local_load_errors:
            from_super = skills_update.get("skills_load_errors", []) if skills_update else []
            baseline = list(dict.fromkeys([*state.get("skills_load_errors", []), *from_super]))
            merged = list(dict.fromkeys([*baseline, *local_load_errors]))
            if merged != baseline:
                if skills_update is None:
                    skills_update = {}
                skills_update["skills_load_errors"] = merged

        if clear_skill_mode:
            return {**(skills_update or {}), "active_skill_mode": None}

        return skills_update

    async def _copy_global_skills(self) -> list[str]:
        """
        Materialize builtin and custom global skills into the virtual ``SKILLS_PATH``,
        sibling to the agent's working directory. Custom global skills override builtins with
        the same name at first cache population.

        Returns source-level load errors so callers can surface them via ``skills_load_errors``.
        """
        files_to_upload: list[tuple[str, bytes]] = []
        errors: list[str] = []
        skills_path = Path(SKILLS_PATH)

        self._collect_skill_files(BUILTIN_SKILLS_PATH, skills_path, files_to_upload, errors)

        custom_skills_path = agent_settings.CUSTOM_SKILLS_PATH
        if custom_skills_path is not None and custom_skills_path.is_dir():
            try:
                self._collect_skill_files(custom_skills_path, skills_path, files_to_upload, errors)
            except OSError as exc:
                # Host path stays in the operator log; the agent gets a generic warning
                # because it cannot act on a real-filesystem location and shouldn't leak it.
                # ``exc.strerror`` carries the OS message without the path (which lives in
                # ``exc.filename``); ``str(exc)`` would include both.
                logger.exception("Failed to read custom global skills from '%s'", custom_skills_path)
                reason = exc.strerror or type(exc).__name__
                errors.append(f"Some custom global skills failed to load: {reason}")
        elif custom_skills_path is not None:
            logger.warning("Custom global skills path '%s' does not exist or is not a directory", custom_skills_path)

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
        Walk skill directories under ``source_root``, appending uploadable files to
        ``files_to_upload`` and ``SKILL.md`` read failures to ``errors`` so a broken
        manifest is not silently dropped from the agent's view.
        """
        for skill_dir in source_root.iterdir():
            if not skill_dir.is_dir() or skill_dir.name == "__pycache__" or skill_dir.name.startswith("."):
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
                    # is a virtual path under ``SKILLS_PATH``, which never exists
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
                        # Surface broken SKILL.md by skill name so the agent can warn a user
                        # who invokes the skill. Host paths stay in the log only — ``exc.strerror``
                        # is the OS message without the path (which lives in ``exc.filename``).
                        if source_path.name == "SKILL.md":
                            reason = exc.strerror or type(exc).__name__
                            errors.append(f"Cannot load skill '{skill_dir.name}': {reason}")

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

            # Skill names can collide between the global tree and per-repo .agents/skills/,
            # so inject the resolved root for relative references inside SKILL.md.
            skill_root = str(Path(loaded_skill["path"]).parent)
            body = (
                f"<skill_root>{skill_root}</skill_root>\n"
                f"Relative paths in this skill resolve under the skill root above.\n\n{body}"
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

            await _record_invocation(name=skill, skill_path=loaded_skill["path"], runtime=runtime)

            return Command(update=update)

        return tool(SKILLS_TOOL_NAME, description=SKILLS_TOOL_DESCRIPTION)(skill_tool)
