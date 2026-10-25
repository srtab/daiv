from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from plugins.runtime import PluginAgentRef, PluginSkillRef

from automation.agent.middlewares.plugins import PluginsMiddleware
from automation.agent.synthetic import synthetic_data, synthetic_kind

TASK_DESCRIPTION = (
    "Launch a subagent.\n\nAvailable agent types:\n- general-purpose: Does anything\n- explorer: Reads code"
    "\n\nUsage notes"
)


def _skill(name: str, description: str = "does it") -> PluginSkillRef:
    return PluginSkillRef(name=name, description=description, path=f"/p/{name}", plugin_root="/p")


def _agent(name: str, description: str = "an agent") -> PluginAgentRef:
    return PluginAgentRef(name=name, description=description, path=f"/p/{name}.md", model=None)


def _tool(name: str, description: str) -> StructuredTool:
    return StructuredTool.from_function(func=lambda: "ok", name=name, description=description)


def _task_description(agents: list[PluginAgentRef]) -> str:
    entries = [
        "- general-purpose: Does anything",
        "- explorer: Reads code",
        *(f"- {a.name}: {a.description}" for a in agents),
    ]
    return "Launch a subagent.\n\nAvailable agent types:\n" + "\n".join(entries) + "\n\nUsage notes"


def _request(messages=None, agents=(), tools=None) -> ModelRequest:
    return ModelRequest(
        model=GenericFakeChatModel(messages=iter([])),
        messages=messages if messages is not None else [HumanMessage(content="hi")],
        system_message=SystemMessage(content="sys"),
        tools=tools if tools is not None else [_tool("task", _task_description(list(agents))), _tool("other", "x")],
    )


async def _run(middleware: PluginsMiddleware, request: ModelRequest) -> tuple[ModelRequest, ModelResponse]:
    seen: list[ModelRequest] = []

    async def handler(req: ModelRequest) -> ModelResponse:
        seen.append(req)
        return ModelResponse(result=[AIMessage(content="ok")])

    response = await middleware.awrap_model_call(request, handler)
    return seen[0], response


def _reminders(response: ModelResponse) -> list:
    return [m for m in response.result if synthetic_kind(m) == "plugins"]


async def test_first_turn_announces_all():
    middleware = PluginsMiddleware(
        [_skill("dev:commit", "commits"), _skill("dev:amend")], [_agent("dev:rev", "reviews")]
    )

    sent, response = await _run(middleware, _request())

    reminder = sent.messages[-1]
    assert synthetic_kind(reminder) == "plugins"
    assert reminder.content == (
        "<plugins>\n"
        "Plugin skills (call with the `skill` tool):\n"
        "- dev:amend: does it\n"
        "- dev:commit: commits\n"
        "Plugin agents (call with the `task` tool, subagent_type = name):\n"
        "- dev:rev: reviews\n"
        "</plugins>"
    )
    assert response.result[0] is reminder
    assert synthetic_data(reminder) == {
        "skills": {"dev:amend": "does it", "dev:commit": "commits"},
        "agents": {"dev:rev": "reviews"},
    }


async def test_empty_sections_are_omitted():
    sent, _ = await _run(PluginsMiddleware([_skill("dev:commit")], []), _request())

    assert "Plugin agents" not in sent.messages[-1].content


async def test_nothing_to_announce_adds_no_reminder():
    sent, response = await _run(PluginsMiddleware([], []), _request())

    assert len(sent.messages) == 1
    assert _reminders(response) == []


async def test_no_reminder_when_unchanged():
    middleware = PluginsMiddleware([_skill("dev:commit")], [_agent("dev:rev")])
    _, first = await _run(middleware, _request())

    sent, response = await _run(
        middleware, _request([HumanMessage(content="hi"), *first.result, AIMessage(content="x")])
    )

    assert len(sent.messages) == 4
    assert _reminders(response) == []


async def test_user_change_announces_diff():
    _, first = await _run(PluginsMiddleware([_skill("dev:commit")], []), _request())
    middleware = PluginsMiddleware([_skill("dev:commit"), _skill("dev:amend")], [_agent("dev:rev")])

    sent, response = await _run(middleware, _request([HumanMessage(content="hi"), *first.result]))

    content = sent.messages[-1].content
    assert "dev:amend" in content
    assert "dev:rev" in content
    assert "dev:commit" not in content
    assert "Now available" in content
    assert synthetic_data(_reminders(response)[0])["skills"] == {"dev:commit": "does it", "dev:amend": "does it"}


async def test_changed_description_is_announced_again():
    _, first = await _run(PluginsMiddleware([_skill("dev:commit", "old")], []), _request())

    sent, _ = await _run(
        PluginsMiddleware([_skill("dev:commit", "new")], []), _request([HumanMessage(content="hi"), *first.result])
    )

    assert "- dev:commit: new" in sent.messages[-1].content


async def test_removed_plugin_listed_as_no_longer_available():
    _, first = await _run(
        PluginsMiddleware([_skill("dev:commit"), _skill("dev:amend")], [_agent("dev:rev")]), _request()
    )

    sent, response = await _run(
        PluginsMiddleware([_skill("dev:amend")], []), _request([HumanMessage(content="hi"), *first.result])
    )

    content = sent.messages[-1].content
    assert "No longer available: dev:commit, dev:rev" in content
    assert "Now available" not in content
    assert synthetic_data(_reminders(response)[0]) == {"skills": {"dev:amend": "does it"}, "agents": {}}


async def test_reannounces_after_reminder_summarized_away():
    middleware = PluginsMiddleware([_skill("dev:commit")], [])
    _, first = await _run(middleware, _request())
    assert first.result

    sent, _ = await _run(middleware, _request([HumanMessage(content="summary")]))

    assert "- dev:commit: does it" in sent.messages[-1].content
    assert "Now available" not in sent.messages[-1].content


async def test_task_description_strips_plugin_agents_only():
    agents = [_agent("dev:rev", "reviews"), _agent("dev:fix", "fixes")]

    sent, _ = await _run(PluginsMiddleware([], agents), _request(agents=agents))

    task = next(t for t in sent.tools if t.name == "task")
    assert task.description == TASK_DESCRIPTION


async def test_task_description_strips_multiline_agent_entries():
    agents = [_agent("dev:rev", "reviews\n  with care\n\nand more"), _agent("dev:fix", "fixes")]

    sent, _ = await _run(PluginsMiddleware([], agents), _request(agents=agents))

    task = next(t for t in sent.tools if t.name == "task")
    assert task.description == TASK_DESCRIPTION


async def test_task_description_keeps_lines_that_only_share_a_prefix():
    request = _request(agents=[_agent("dev:rev", "reviews")])

    sent, _ = await _run(PluginsMiddleware([], [_agent("dev:rev", "other text")]), request)

    task = next(t for t in sent.tools if t.name == "task")
    assert "- dev:rev: reviews" in task.description


async def test_non_task_tools_and_dict_tools_pass_through():
    other = _tool("other", "x")
    raw = {"type": "function", "function": {"name": "task"}}

    sent, _ = await _run(PluginsMiddleware([], [_agent("dev:rev")]), _request(tools=[other, raw]))

    assert sent.tools == [other, raw]


async def test_system_prompt_and_tools_identical_across_plugin_changes():
    first_agents = [_agent("dev:rev")]
    second_agents = [_agent("dev:fix", "fixes\nmore"), _agent("dev:rev")]

    first, _ = await _run(PluginsMiddleware([_skill("dev:a")], first_agents), _request(agents=first_agents))
    second, _ = await _run(PluginsMiddleware([_skill("dev:b")], second_agents), _request(agents=second_agents))

    assert first.system_message.content == second.system_message.content
    assert [(t.name, t.description) for t in first.tools] == [(t.name, t.description) for t in second.tools]
