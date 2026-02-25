from typing import TYPE_CHECKING

from langchain.messages import AIMessage

from automation.agent.constants import ModelName

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage
    from langchain_core.tools import ToolCall

INTERRUPT_ALL_TOOLS_CONFIG = {
    # SkillMiddleware
    "skill": True,
    # TodoListMiddleware
    "write_todos": True,
    # FilesystemMiddleware
    "grep": True,
    "glob": True,
    "ls": True,
    "read_file": True,
    "edit_file": True,
    "write_file": True,
    # SubAgentMiddleware
    "task": True,
    # SandboxMiddleware
    "bash": True,
    # WebFetchMiddleware
    "web_fetch": True,
    # WebSearchMiddleware
    "web_search": True,
    # GitPlatformMiddleware
    "github": True,
    "gitlab": True,
}

CODING_MODEL_NAMES = [
    ModelName.CLAUDE_SONNET_4_5,
    ModelName.CLAUDE_SONNET_4_6,
    ModelName.CLAUDE_OPUS_4_5,
    ModelName.CLAUDE_OPUS_4_6,
    ModelName.GPT_5_2,
    ModelName.GPT_5_3_CODEX,
    ModelName.Z_AI_GLM_5,
    ModelName.MINIMAX_M2_5,
    ModelName.MOONSHOTAI_KIMI_K2_5,
]

FAST_MODEL_NAMES = [ModelName.CLAUDE_HAIKU_4_5, ModelName.GPT_5_1_CODEX_MINI, ModelName.GPT_4_1]


def extract_tool_calls(messages: list[BaseMessage]) -> list[ToolCall]:
    return [tool_call for message in messages if isinstance(message, AIMessage) for tool_call in message.tool_calls]
