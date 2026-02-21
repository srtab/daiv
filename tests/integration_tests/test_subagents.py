import pytest
from deepagents.backends import StoreBackend
from langchain.agents import create_agent
from langchain.tools import ToolRuntime
from langgraph.store.memory import InMemoryStore
from langsmith import testing as t

from automation.agent import BaseAgent
from automation.agent.subagents import create_docs_research_subagent

from .utils import FAST_MODEL_NAMES, extract_tool_calls

TEST_SUITE = "DAIV: Subagents"


async def run_subagent(model_name: str, query: str) -> dict:
    model = BaseAgent.get_model(model=model_name)
    runtime = ToolRuntime(
        state={}, config={}, stream_writer=None, tool_call_id="test_call_1", store=InMemoryStore(), context={}
    )
    subagent_spec = create_docs_research_subagent(backend=StoreBackend(runtime=runtime))
    subagent = create_agent(
        model,
        system_prompt=subagent_spec["system_prompt"],
        tools=subagent_spec["tools"],
        middleware=subagent_spec["middleware"],
        name=subagent_spec["name"],
    )
    return await subagent.ainvoke({"messages": [{"role": "user", "content": query}]})


def _assert_typical_execution_result(result: dict):
    tool_calls = extract_tool_calls(result)
    assert len(tool_calls) >= 2
    assert all(tool_call["name"] == "web_fetch" for tool_call in tool_calls)
    assert all(tool_call["args"]["prompt"] == "" for tool_call in tool_calls)
    assert "### Answer" in result["messages"][-1].content, result["messages"][-1].pretty_repr()
    assert "### Code Example" in result["messages"][-1].content, result["messages"][-1].pretty_repr()
    assert "### Notes" in result["messages"][-1].content, result["messages"][-1].pretty_repr()
    assert "### Source" in result["messages"][-1].content, result["messages"][-1].pretty_repr()


@pytest.mark.subagents
@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
@pytest.mark.parametrize("model_name", FAST_MODEL_NAMES)
@pytest.mark.parametrize(
    "query",
    [
        # Typical cases
        "How do I use useReducer in React?",
        "How does Next.js handle environment variables?",
        "Show me how to define a route in FastAPI",
        "How do I make a POST request with axios?",
        # Edge cases for which we should never use training knowledge
        "What's new in React 19?",
        "How does Next.js 15 handle caching differently from Next.js 14?",
    ],
)
async def test_docs_research_subagent_typical_execution(model_name, query):
    t.log_inputs({"model_name": model_name, "query": query})

    result = await run_subagent(model_name, query)

    t.log_outputs(result)

    _assert_typical_execution_result(result)


@pytest.mark.subagents
@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
@pytest.mark.parametrize("model_name", FAST_MODEL_NAMES)
@pytest.mark.parametrize(
    "query, expected_tool_calls",
    [
        pytest.param("How are you doing?", 0, id="how-are-you-doing"),
        pytest.param("How do I use async/await?", 0, id="how-do-i-use-async-await"),
        pytest.param("How do I use django tasks on version 10?", None, id="how-do-i-use-django-tasks-on-version-10"),
    ],
)
async def test_docs_research_subagent_ask_clarifying_questions(model_name, query, expected_tool_calls):
    """
    Test the rules defined in the Quality Standards section: must confirm versions before fetching documentation.
    """

    t.log_inputs({"model_name": model_name, "query": query})

    result = await run_subagent(model_name, query)

    t.log_outputs(result)

    tool_calls = extract_tool_calls(result)

    if expected_tool_calls is not None:
        assert len(tool_calls) == expected_tool_calls

    assert all(tool_call["name"] == "web_fetch" for tool_call in tool_calls)
    assert all(tool_call["args"]["prompt"] == "" for tool_call in tool_calls)

    if expected_tool_calls == 0:
        # This means that the model did not fetch any documentation and asked a clarifying question instead.
        assert "### Answer" not in result["messages"][-1].content, result["messages"][-1].pretty_repr()
        assert "### Code Example" not in result["messages"][-1].content, result["messages"][-1].pretty_repr()
        assert "### Notes" not in result["messages"][-1].content, result["messages"][-1].pretty_repr()
        assert "### Source" not in result["messages"][-1].content, result["messages"][-1].pretty_repr()
    elif expected_tool_calls is None:
        # This means that the model fetched documentation and answered the question directly.
        assert "### Answer" in result["messages"][-1].content, result["messages"][-1].pretty_repr()
        assert "### Code Example" in result["messages"][-1].content, result["messages"][-1].pretty_repr()
        assert "### Notes" in result["messages"][-1].content, result["messages"][-1].pretty_repr()
        assert "### Source" in result["messages"][-1].content, result["messages"][-1].pretty_repr()
