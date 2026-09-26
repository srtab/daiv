import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from automation.agent.questions import (
    ASK_USER_QUESTION_TOOL_NAME,
    QUESTION_DELIVERED,
    AskUserQuestionInput,
    delivered_question_call,
    is_question_close,
    pending_question,
    render_questions,
)
from tests.unit_tests.conftest import SAMPLE_QUESTION_PAYLOAD, ask_user_question_messages


def _question(**overrides):
    base = {"header": "Scope", "question": "Which modules should change?", "options": [], "multi_select": False}
    return {**base, **overrides}


def _option(label="Yes", description="Do it."):
    return {"label": label, "description": description}


class TestSchema:
    def test_the_sample_payload_is_valid(self):
        assert AskUserQuestionInput.model_validate(SAMPLE_QUESTION_PAYLOAD).model_dump() == SAMPLE_QUESTION_PAYLOAD

    @pytest.mark.parametrize("count", [0, 5])
    def test_one_to_four_questions(self, count):
        with pytest.raises(ValidationError):
            AskUserQuestionInput.model_validate({"questions": [_question()] * count})

    @pytest.mark.parametrize("header", ["", "x" * 13])
    def test_header_is_one_to_twelve_characters(self, header):
        with pytest.raises(ValidationError):
            AskUserQuestionInput.model_validate({"questions": [_question(header=header)]})

    def test_question_must_end_with_a_question_mark(self):
        with pytest.raises(ValidationError):
            AskUserQuestionInput.model_validate({"questions": [_question(question="Pick a module.")]})

    def test_a_full_width_question_mark_is_accepted(self):
        AskUserQuestionInput.model_validate({"questions": [_question(question="どれを使いますか？")]})

    @pytest.mark.parametrize("count", [1, 5])
    def test_options_are_zero_or_two_to_four(self, count):
        with pytest.raises(ValidationError):
            AskUserQuestionInput.model_validate({
                "questions": [_question(options=[_option(f"o{i}") for i in range(count)])]
            })

    def test_option_label_is_at_most_five_words(self):
        options = [_option("one two three four five six"), _option("No")]
        with pytest.raises(ValidationError):
            AskUserQuestionInput.model_validate({"questions": [_question(options=options)]})

    def test_option_description_is_required(self):
        with pytest.raises(ValidationError):
            AskUserQuestionInput.model_validate({
                "questions": [_question(options=[_option(description=""), _option("No")])]
            })


class TestRender:
    def test_renders_header_question_numbered_options_and_the_free_text_hint(self):
        assert render_questions(SAMPLE_QUESTION_PAYLOAD) == (
            "**Database**: Which database engine should the project move to?\n\n"
            "1. **PostgreSQL**: Keep a relational store with the richest Django support.\n"
            "2. **SQLite**: Single-file database, simplest to run.\n\n"
            "_If none of the options fit, reply in your own words._"
        )

    def test_multi_select_says_so_and_the_hint_closes_the_message(self):
        payload = {
            "questions": [
                _question(header="Targets", options=[_option("API"), _option("UI")], multi_select=True),
                _question(header="Deadline", question="When is it due?"),
            ]
        }
        assert render_questions(payload) == (
            "**Targets**: Which modules should change?\n\n"
            "1. **API**: Do it.\n"
            "2. **UI**: Do it.\n\n"
            "_Pick any that apply._\n\n"
            "**Deadline**: When is it due?\n\n"
            "_If none of the options fit, reply in your own words._"
        )


class TestDerivation:
    def test_a_turn_that_ended_on_a_question_is_pending(self):
        messages = [HumanMessage(content="migrate the db"), *ask_user_question_messages()]
        assert pending_question(messages) == SAMPLE_QUESTION_PAYLOAD

    def test_the_answer_clears_the_pending_question(self):
        messages = [*ask_user_question_messages(), HumanMessage(content="**Database** — SQLite")]
        assert pending_question(messages) is None

    def test_a_failed_delivery_is_not_pending(self):
        call, _delivered, close = ask_user_question_messages()
        failed = ToolMessage(content="boom", tool_call_id="ask-1", name=ASK_USER_QUESTION_TOOL_NAME, status="error")
        assert pending_question([call, failed, close]) is None

    def test_a_call_with_siblings_is_never_delivered(self):
        call = AIMessage(
            content="",
            tool_calls=[
                {"id": "ask-1", "name": ASK_USER_QUESTION_TOOL_NAME, "args": SAMPLE_QUESTION_PAYLOAD},
                {"id": "ls-1", "name": "ls", "args": {"path": "/"}},
            ],
        )
        delivered = ToolMessage(content=QUESTION_DELIVERED, tool_call_id="ask-1", name=ASK_USER_QUESTION_TOOL_NAME)
        assert delivered_question_call([call, delivered]) is None

    def test_delivered_call_is_the_last_two_messages(self):
        call, delivered, _close = ask_user_question_messages()
        assert delivered_question_call([call, delivered])["id"] == "ask-1"
        assert delivered_question_call([call]) is None

    def test_is_question_close_marks_only_the_close_message(self):
        messages = [HumanMessage(content="hi"), *ask_user_question_messages()]
        assert [is_question_close(messages, i) for i in range(len(messages))] == [False, False, False, True]
