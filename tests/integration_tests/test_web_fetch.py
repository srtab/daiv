from django.core.cache import cache

import pytest
from langsmith import testing as t

from automation.agent.middlewares.web_fetch import _get_cached_response, web_fetch_tool
from core.site_settings import site_settings

TEST_SUITE = "DAIV: Web Fetch"


@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
@pytest.mark.parametrize(
    "inputs,expected_terms",
    [
        pytest.param(
            {
                "url": "https://pypi.org/project/langchain/",
                "prompt": "What is the package name in this page? Return only the package name.",
            },
            ["langchain"],
            id="extract-package-name-from-pypi-page",
        ),
        pytest.param(
            {
                "url": "https://srtab.github.io/daiv/latest",
                "prompt": "What project is this documentation for? Return only the project name.",
            },
            ["daiv"],
            id="extract-project-name-from-latest-docs-url",
        ),
    ],
)
async def test_web_fetch_realistic_extraction_prompts(inputs, expected_terms):
    cache.clear()
    t.log_inputs(inputs)

    result = await web_fetch_tool.ainvoke(inputs)
    t.log_outputs({"result": result})

    result_lower = result.lower()
    assert any(term in result_lower for term in expected_terms), result


@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
async def test_web_fetch_returns_raw_contents_when_prompt_is_empty():
    cache.clear()
    inputs = {"url": "https://example.com", "prompt": ""}
    t.log_inputs(inputs)

    result = await web_fetch_tool.ainvoke(inputs)
    t.log_outputs({"result": result})

    assert result.startswith("Contents of https://example.com"), result
    assert "example domain" in result.lower(), result


@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
async def test_web_fetch_reuses_cached_response_for_same_url_and_prompt():
    if site_settings.web_fetch_model_name is None:
        pytest.skip(
            "WEB_FETCH_MODEL_NAME is not configured; response caching is only used for model-processed prompts."
        )

    cache.clear()
    inputs = {"url": "https://pypi.org/project/langchain/", "prompt": "Summarize this page in one short sentence."}
    t.log_inputs(inputs)

    first_result = await web_fetch_tool.ainvoke(inputs)
    cached_result = _get_cached_response(url=inputs["url"], prompt=inputs["prompt"])
    second_result = await web_fetch_tool.ainvoke(inputs)
    t.log_outputs({"first_result": first_result, "second_result": second_result, "cached_result": cached_result})

    assert cached_result is not None
    assert second_result == first_result
