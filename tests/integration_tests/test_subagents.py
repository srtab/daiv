import pytest
from deepagents.backends import StoreBackend
from langchain.agents import create_agent
from langchain.tools import ToolRuntime
from langgraph.store.memory import InMemoryStore
from langsmith import testing as t

from automation.agent import BaseAgent, ThinkingLevel
from automation.agent.subagents import create_docs_research_subagent
from automation.agent.utils import extract_text_content

from .evaluators import correctness_evaluator
from .utils import FAST_MODEL_NAMES, extract_tool_calls

TEST_SUITE = "DAIV: Subagents"


async def run_subagent(model_name: str, query: str) -> dict:
    model = BaseAgent.get_model(model=model_name, thinking_level=ThinkingLevel.MEDIUM)
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

    tool_calls = extract_tool_calls(result["messages"])
    text_content = extract_text_content(result["messages"][-1].content)
    assert len(tool_calls) >= 2
    assert all(tool_call["name"] == "web_fetch" for tool_call in tool_calls), f"Tool calls: {tool_calls}"
    assert all(tool_call["args"]["prompt"] == "" for tool_call in tool_calls), f"Tool calls: {tool_calls}"
    assert "### Answer" in text_content
    assert "### Notes" in text_content
    assert "### Source" in text_content

    result = await correctness_evaluator(
        inputs={"query": query}, outputs={"messages": result["messages"]}, reference_outputs=""
    )
    assert result["score"] is True, result["comment"]


@pytest.mark.subagents
@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
@pytest.mark.parametrize("model_name", FAST_MODEL_NAMES)
@pytest.mark.parametrize(
    "query, expected_tool_calls, reference_outputs",
    [
        pytest.param(
            "How are you doing?",
            0,
            [
                "Missing context: no programming language or framework was specified. Please include the target language or framework (e.g., Python, JavaScript, Rust, Django) in your query to unlock this request.",  # noqa: E501
                "I'm doing well, thanks for asking! 👋\n\nI'm here and ready to help you with **documentation research**. I specialize in fetching up-to-date documentation from various libraries and frameworks using the Context7 API.\n\nIf you have a question about how to use a specific library or framework (like React, FastAPI, Django, Next.js, etc.), just ask and I'll:\n- Find the correct documentation\n- Pull the relevant code examples\n- Explain any important caveats or version requirements\n\nWhat can I help you with today?",  # noqa: E501
            ],
            id="how-are-you-doing",
        ),
        pytest.param(
            "How do I use async/await?",
            0,
            [
                "Missing context: no programming language or framework was specified. Please include the target language or framework (e.g., Python, JavaScript, Rust, Django) in your query to unlock this request."  # noqa: E501
            ],
            id="how-do-i-use-async-await",
        ),
        pytest.param(
            "How do I use django tasks on version 10?",
            None,
            [
                '## Django — django tasks\n\n### Answer\nIn Django 6.0 (closest available docs to version 10), background work is defined as a module-level function decorated with `django.tasks.task`; this decorator can accept options such as `priority` and `queue_name`. Once defined, you call the task’s `.enqueue()` method with the same arguments you declared to queue it for execution, and an external worker process is responsible for running the task.\n\n### Code Example\n```python\nfrom django.core.mail import send_mail\nfrom django.tasks import task\n\n\n@task\ndef email_users(emails, subject, message):\n    return send_mail(\n        subject=subject, message=message, from_email=None, recipient_list=emails\n    )\n```\n\n```python\nresult = email_users.enqueue(\n    emails=["user@example.com"],\n    subject="You have a message",\n    message="Hello there!",\n)\n```\n\n### Notes\nDocumentation is sourced from Django 6.0 because version 10 materials are not available in Context7; behavior in Django 10 may differ.\n\n### Source\nLibrary ID: /websites/djangoproject_en_6_0',  # noqa: E501
                "I notice that Django version 10 does not exist in the current search results. The available versions shown are Django 2.2, 4.2, 5.2, and 6.0. \n\n**Missing context:** Django version 10 does not exist. The latest available version is Django 6.0. Did you mean:\n- **Django 1.0** (the original release from 2008)?\n- **Django 5.0 or 6.0** (the current latest versions)?\n\nPlease clarify which Django version you want to use with tasks, and I'll fetch the correct documentation for you.",  # noqa: E501
            ],
            id="how-do-i-use-django-tasks-on-version-10",
        ),
    ],
)
async def test_docs_research_subagent_ask_clarifying_questions(
    model_name, query, expected_tool_calls, reference_outputs
):
    """
    Test the rules defined in the Quality Standards section: must confirm versions before fetching documentation.
    """

    t.log_inputs({"model_name": model_name, "query": query})

    result = await run_subagent(model_name, query)

    t.log_outputs(result)

    tool_calls = extract_tool_calls(result["messages"])

    if expected_tool_calls is not None:
        assert len(tool_calls) == expected_tool_calls

    assert all(tool_call["name"] == "web_fetch" for tool_call in tool_calls)
    assert all(tool_call["args"]["prompt"] == "" for tool_call in tool_calls)

    result = await correctness_evaluator(
        inputs={"query": query}, outputs={"messages": result["messages"]}, reference_outputs=reference_outputs
    )
    assert result["score"] is True, result["comment"]
