"""Announce the turn's plugin skills and agents without touching the prompt-cache prefix.

The system prompt and the tools array must stay byte-identical between turns, so plugin skills and agents reach
the model only as a persisted reminder (see ``reminders``). The reminder carries the announced set as data; a
later model call compares it with the turn's set and sends a new reminder only when they differ. The plugin
agents are compiled into the ``task`` tool, so their lines are stripped from its description before each call.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import BaseTool

from automation.agent.middlewares.reminders import call_with_reminder
from automation.agent.synthetic import synthetic_data, synthetic_kind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain.agents.middleware.types import ModelCallResult
    from plugins.runtime import PluginAgentRef, PluginSkillRef

PLUGINS_KIND = "plugins"
TASK_TOOL_NAME = "task"

Announced = dict[str, dict[str, str]]


def _announced(messages: Sequence[Any]) -> Announced:
    for message in reversed(messages):
        if synthetic_kind(message) == PLUGINS_KIND:
            data = synthetic_data(message) or {}
            return {"skills": dict(data.get("skills", {})), "agents": dict(data.get("agents", {}))}
    return {"skills": {}, "agents": {}}


def _entries(items: Mapping[str, str]) -> list[str]:
    return [f"- {name}: {description}" for name, description in sorted(items.items())]


def _reminder_text(announced: Announced, current: Announced) -> str:
    changed = {
        kind: {name: desc for name, desc in current[kind].items() if announced[kind].get(name) != desc}
        for kind in current
    }
    removed = sorted(name for kind in announced for name in announced[kind] if name not in current[kind])

    lines = ["<plugins>"]
    if any(announced.values()) and any(changed.values()):
        lines.append("Now available:")
    if changed["skills"]:
        lines += ["Plugin skills (call with the `skill` tool):", *_entries(changed["skills"])]
    if changed["agents"]:
        lines += ["Plugin agents (call with the `task` tool, subagent_type = name):", *_entries(changed["agents"])]
    if removed:
        lines.append(f"No longer available: {', '.join(removed)}")
    lines.append("</plugins>")
    return "\n".join(lines)


def _strip_agents(description: str, agents: Sequence[PluginAgentRef]) -> str:
    for agent in agents:
        entry = re.escape(f"- {agent.name}: {agent.description}")
        description = re.sub(rf"(?:^|\n){entry}(?=\n|$)", "", description, count=1)
    return description


class PluginsMiddleware(AgentMiddleware):
    """Tell the model about the turn's plugin skills and agents through reminders only."""

    def __init__(self, skills: Sequence[PluginSkillRef], agents: Sequence[PluginAgentRef]) -> None:
        super().__init__()
        self._agents = list(agents)
        self._current: Announced = {
            "skills": {skill.name: skill.description for skill in skills},
            "agents": {agent.name: agent.description for agent in agents},
        }

    def _without_plugin_agents(self, tools: Sequence[Any]) -> list[Any]:
        stripped = []
        for tool in tools:
            if isinstance(tool, BaseTool) and tool.name == TASK_TOOL_NAME:
                tool = tool.model_copy(update={"description": _strip_agents(tool.description, self._agents)})
            stripped.append(tool)
        return stripped

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        if self._agents:
            request = request.override(tools=self._without_plugin_agents(request.tools))

        announced = _announced(request.messages)
        if announced == self._current:
            return await handler(request)
        return await call_with_reminder(
            request, handler, _reminder_text(announced, self._current), kind=PLUGINS_KIND, data=self._current
        )
