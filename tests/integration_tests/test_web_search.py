from django.utils import timezone

import pytest
from langsmith import testing as t

from automation.agent.middlewares.web_search import WEB_SEARCH_NAME

from .coverage_grading import search_backend_failed, trailing_source_links
from .prompt_leak import assert_no_prompt_leak
from .utils import (
    WEB_SEARCH_MODELS,
    extract_tool_calls,
    final_text,
    measure,
    require_provider_for_model,
    run_agent_once,
)

pytestmark = [
    pytest.mark.web_search,
    pytest.mark.langsmith(test_suite_name="DAIV: Web search"),
    pytest.mark.parametrize("model_name", WEB_SEARCH_MODELS),
]

QUESTION = "What is the latest released version of Django?"

assert_no_prompt_leak([QUESTION])

WRITE_INTERRUPTS = {"edit_file": True, "write_file": True, "bash": True, "task": True}


async def test_a_search_answer_uses_the_current_year_and_ends_with_sources(model_name, eval_request):
    require_provider_for_model(model_name)
    t.log_inputs({"model_name": model_name, "prompt": QUESTION})

    with measure(eval_request) as metrics:
        result = await run_agent_once(model_name, QUESTION, interrupt_on=WRITE_INTERRUPTS)
    messages = metrics.messages = result["messages"]
    t.log_outputs(result)

    searches = [call for call in extract_tool_calls(messages) if call["name"] == WEB_SEARCH_NAME]
    assert searches, "Expected a web_search call"
    if search_backend_failed(messages, WEB_SEARCH_NAME):
        pytest.skip("Every web_search call failed in the search backend; that is not a prompt outcome.")

    year = str(timezone.now().year)
    queries = [str(call["args"].get("query", "")) for call in searches]
    assert any(year in query for query in queries), f"Expected the current year ({year}) in a query, got {queries}"
    reply = final_text(messages)
    assert trailing_source_links(reply), (
        f"Expected the reply to end with a Sources: list of links, got: {reply[-600:]!r}"
    )
