from types import SimpleNamespace

from automation.agent.tool_media import ToolContent


def test_text_joins_the_text_of_each_block():
    content = [{"type": "text", "text": "a"}, {"type": "image", "base64": "AAAA"}, "b", {"text": "c"}, {"text": None}]

    assert ToolContent(SimpleNamespace(content=content)).text == "a\nb\nc"
    assert ToolContent(SimpleNamespace(content="plain")).text == "plain"
