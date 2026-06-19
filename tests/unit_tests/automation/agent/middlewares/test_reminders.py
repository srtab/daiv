from langchain.agents.middleware import ModelRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import HumanMessage

from automation.agent.middlewares.reminders import append_system_reminder


def _request() -> ModelRequest:
    return ModelRequest(model=GenericFakeChatModel(messages=iter([])), messages=[HumanMessage(content="hi")])


def test_appends_text_as_trailing_message():
    request = _request()
    new_request = append_system_reminder(request, "<system-reminder>x</system-reminder>")
    assert len(new_request.messages) == 2
    assert new_request.messages[-1].content == "<system-reminder>x</system-reminder>"


def test_original_request_is_untouched():
    request = _request()
    append_system_reminder(request, "ephemeral")
    assert len(request.messages) == 1
