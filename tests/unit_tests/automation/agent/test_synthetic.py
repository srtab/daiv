from langchain_core.messages import AIMessage, HumanMessage

from automation.agent.synthetic import SYNTHETIC_KWARG, is_person_message, is_synthetic, synthetic_message
from core.checkpointer import DAIVRedisSerializer


def test_synthetic_message_is_marked_with_its_kind():
    message = synthetic_message("<system-reminder>x</system-reminder>", kind="step_budget")

    assert isinstance(message, HumanMessage)
    assert message.content == "<system-reminder>x</system-reminder>"
    assert message.additional_kwargs == {SYNTHETIC_KWARG: "step_budget"}
    assert is_synthetic(message)


def test_synthetic_message_gets_a_fresh_id_up_front():
    first = synthetic_message("x", kind="loop_breaker")
    second = synthetic_message("x", kind="loop_breaker")

    assert first.id
    assert first.id != second.id


def test_person_written_messages_are_not_synthetic():
    assert not is_synthetic(HumanMessage(content="fix the bug"))
    assert not is_synthetic({"role": "user", "content": "fix the bug"})


def test_mark_survives_the_checkpoint_round_trip():
    serde = DAIVRedisSerializer()
    original = synthetic_message("x", kind="empty_response")

    restored = serde.loads_typed(serde.dumps_typed({"messages": [original]}))["messages"][0]

    assert is_synthetic(restored)
    assert restored.id == original.id


def test_synthetic_message_keeps_a_given_id():
    message = synthetic_message("x", kind="issue_context", message_id="issue-context-42-abc")
    assert message.id == "issue-context-42-abc"


def test_only_unmarked_human_messages_are_person_messages():
    assert is_person_message(HumanMessage(content="fix the bug"))
    assert not is_person_message(synthetic_message("x", kind="step_budget"))
    assert not is_person_message(AIMessage(content="done"))
    assert not is_person_message({"role": "user", "content": "fix the bug"})
