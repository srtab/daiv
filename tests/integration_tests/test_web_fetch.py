from __future__ import annotations

from django.core.cache import cache

from automation.agent.middlewares.web_fetch import web_fetch_tool


async def test_web_fetch_extracts_expected_content():
    cache.clear()

    result = await web_fetch_tool.ainvoke({
        "url": "https://pypi.org/project/langchain/",
        "prompt": "Extract the name of the package.",
    })
    assert "langchain" in result.lower()


async def test_same_host_redirect_is_followed():
    cache.clear()

    result = await web_fetch_tool.ainvoke({
        "url": "https://srtab.github.io/daiv/latest",
        "prompt": "Is this the latest documentation?",
    })
    assert "yes" in result.lower()
