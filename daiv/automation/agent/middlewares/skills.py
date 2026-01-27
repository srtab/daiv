import logging
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, override

from deepagents.middleware.skills import SkillMetadata, SkillsState, SkillsStateUpdate
from deepagents.middleware.skills import SkillsMiddleware as DeepAgentsSkillsMiddleware
from langchain.agents.middleware import hook_config
from langchain.tools import ToolRuntime, tool  # noqa: TC002
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime  # noqa: TC002

from automation.agent.constants import BUILTIN_SKILLS_PATH, DAIV_SKILLS_PATH
from automation.agent.utils import extract_body_from_frontmatter, extract_text_content
from codebase.context import RuntimeCtx  # noqa: TC001
from slash_commands.parser import SlashCommandCommand, parse_slash_command
from slash_commands.registry import slash_command_registry

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langchain_core.tools import BaseTool


logger = logging.getLogger("daiv.tools")

SKILL_ARGUMENTS_PLACEHOLDER = "$ARGUMENTS"

SKILLS_TOOL_NAME = "skill"
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
</example>

**Important:**
- When a skill is relevant, you must invoke the `{SKILLS_TOOL_NAME}` tool IMMEDIATELY as your first action.
- NEVER just announce or mention a skill in your text response without actually calling the `{SKILLS_TOOL_NAME}` tool.
- This is a BLOCKING REQUIREMENT: invoke the relevant `{SKILLS_TOOL_NAME}` tool BEFORE generating any other response about the task.
- Only use skills listed in <available_skills> below.
- Do not invoke a skill that is already running.

{{skills_list}}"""  # noqa: E501


AVAILABLE_SKILLS_TEMPLATE = PromptTemplate.from_template(
    """<available_skills>
  {{#skills_list}}
  <skill>
    <name>{{name}}</name>
    <description>{{description}}</description>
    {{#metadata.is_builtin}}
    <builtin>true</builtin>
    {{/metadata.is_builtin}}
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.system_prompt_template = SKILLS_SYSTEM_PROMPT
        self.tools = [self._skill_tool_generator()]

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(
        self, state: SkillsState, runtime: Runtime[RuntimeCtx], config: RunnableConfig
    ) -> SkillsStateUpdate | None:
        """
        Apply builtin slash commands early in the conversation and copy builtin skills to the project skills directory
        to make them available to the agent.
        """
        if "skills_metadata" in state:
            return None

        builtin_skills = await self._copy_builtin_skills(agent_path=Path(runtime.context.repo.working_dir))

        skills_update = await super().abefore_agent(state, runtime, config)

        # Mark builtin skills as builtin. Unmark non-builtin skills. This is necessary because the builtin skills can
        # be rewritten to the project skills directory by the user and they should not be marked as builtin anymore.
        if skills_update is not None:
            for skill in skills_update["skills_metadata"]:
                if skill["name"] in builtin_skills:
                    skill["metadata"]["is_builtin"] = True
                else:
                    skill["metadata"].pop("is_builtin", None)

        builtin_slash_commands = await self._apply_builtin_slash_commands(
            state["messages"], runtime.context, skills_update["skills_metadata"]
        )

        if builtin_slash_commands:
            return builtin_slash_commands

        return skills_update

    async def _copy_builtin_skills(self, agent_path: Path) -> list[str]:
        """
        Copy builtin skills to the project skills directory if they don't exist.

        This allows the agent to find built-in skills and execute scripts bundled with them as if they were project
        skills, even if the project skills directory is not set up. The copied skills folder includes a .gitignore file
        to prevent the skills from being committed to the repository.

        Users can override built-in skills by creating them with the same name in the project skills directory and
        committing them to the repository.

        Args:
            agent_path: The path to the agent's repository.

        Returns:
            A list of builtin skill names.
        """
        builtin_skills = []
        files_to_upload = []
        project_skills_path = Path(f"/{agent_path.name}/{DAIV_SKILLS_PATH}")

        for builtin_skill_dir in BUILTIN_SKILLS_PATH.iterdir():
            if not builtin_skill_dir.is_dir() or builtin_skill_dir.name == "__pycache__":
                continue

            builtin_skills.append(builtin_skill_dir.name)

            for root, _dirs, files in builtin_skill_dir.walk():
                for file in files:
                    source_path = Path(root) / Path(file)
                    dest_path = project_skills_path / source_path.relative_to(BUILTIN_SKILLS_PATH)
                    if not dest_path.exists():
                        files_to_upload.append((str(dest_path), source_path.read_text().encode("utf-8")))

            dest_path = project_skills_path / builtin_skill_dir.relative_to(BUILTIN_SKILLS_PATH)
            files_to_upload.append((str(dest_path / ".gitignore"), b"*"))

        for response in await self._backend.aupload_files(files_to_upload):
            if response.error:
                raise RuntimeError(f"Failed to upload builtin skill: {response.error}")
        return builtin_skills

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

        command = command_classes[0](scope=context.scope, repo_id=context.repo_id, bot_username=context.bot_username)
        logger.info("[%s] Executing `%s` slash command", self.name, slash_command.raw)

        try:
            result = await command.execute_for_agent(
                args=" ".join(slash_command.args),
                issue_iid=context.issue.iid if context.issue else None,
                merge_request_id=context.merge_request.merge_request_id if context.merge_request else None,
                available_skills=skills,
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
        """
        Generate a skill tool.

        Args:
            backend: The backend to read the skill from.

        Returns:
            A BaseTool.
        """

        async def skill_tool(
            skill: Annotated[str, "The skill name. E.g. 'code-review' or 'web-research'"],
            runtime: ToolRuntime[RuntimeCtx, SkillsState],
            skill_args: Annotated[str | None, "Optional arguments to pass to the skill."] = None,
        ) -> str:
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

            # Positional args like $1, $2
            for i, a in enumerate(shlex.split(skill_args or ""), start=1):
                body = body.replace(f"${i}", a).replace(f"{SKILL_ARGUMENTS_PLACEHOLDER}[{i}]", a)

            # Named args, only $ARGUMENTS supported
            if skill_args and (arg_str := skill_args.strip()):
                body = (
                    body.replace(SKILL_ARGUMENTS_PLACEHOLDER, arg_str)
                    if SKILL_ARGUMENTS_PLACEHOLDER in body
                    else f"{body}\n\n{SKILL_ARGUMENTS_PLACEHOLDER}: {arg_str}"
                )

            return [
                ToolMessage(content=f"Launching skill '{skill}'...", tool_call_id=runtime.tool_call_id),
                HumanMessage(content=body),
            ]

        return tool(SKILLS_TOOL_NAME, description=SKILLS_TOOL_DESCRIPTION)(skill_tool)
