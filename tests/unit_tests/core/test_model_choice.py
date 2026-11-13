import logging

import pytest

from core.model_choice import EMPTY_CHOICE, ModelChoice, merge_model_choices, parse_model_choice, parse_model_choices


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("openai:gpt-5.4", ModelChoice(model="openai:gpt-5.4")),
        ({"thinking_level": "HIGH"}, ModelChoice(thinking_level="high")),
        ({"model": "openai:gpt-5.4", "thinking_level": "off"}, ModelChoice("openai:gpt-5.4", "off")),
        ({"model": None, "thinking_level": None}, EMPTY_CHOICE),
        (ModelChoice(model="x:y"), ModelChoice(model="x:y")),
        ("", EMPTY_CHOICE),
        (None, EMPTY_CHOICE),
    ],
)
def test_parse_model_choice(value, expected):
    assert parse_model_choice(value, source="test") == expected


def test_an_unknown_level_is_dropped_and_logged_keeping_the_model(caplog):
    with caplog.at_level(logging.ERROR):
        choice = parse_model_choice({"model": "x:y", "thinking_level": "extreme"}, source=".daiv.yml")
    assert choice == ModelChoice(model="x:y")
    assert "extreme" in caplog.text
    assert ".daiv.yml" in caplog.text


def test_log_level_sets_the_level_of_the_drop_log(caplog):
    with caplog.at_level(logging.DEBUG):
        parse_model_choice({"thinking_level": "extreme"}, source="f", log_level=logging.WARNING)
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_a_non_string_model_is_dropped_and_logged_keeping_the_level(caplog):
    with caplog.at_level(logging.ERROR):
        choice = parse_model_choice({"model": 7, "thinking_level": "low"}, source="test")
    assert choice == ModelChoice(thinking_level="low")
    assert caplog.records


def test_a_value_that_is_neither_string_nor_mapping_is_dropped_and_logged(caplog):
    with caplog.at_level(logging.ERROR):
        assert parse_model_choice(42, source="test") == EMPTY_CHOICE
    assert caplog.records


def test_parse_model_choices_drops_empty_entries_and_non_string_names():
    parsed = parse_model_choices({"a": "x:y", "b": None, "c": {}, 3: "x:y"}, source="test")
    assert parsed == {"a": ModelChoice(model="x:y")}


def test_parse_model_choices_of_a_non_mapping_is_empty_and_logged(caplog):
    with caplog.at_level(logging.ERROR):
        assert parse_model_choices(["x:y"], source="test") == {}
    assert caplog.records


def test_over_takes_each_field_from_the_first_that_sets_it():
    assert ModelChoice(model="a:b").over(ModelChoice("c:d", "low")) == ModelChoice("a:b", "low")


@pytest.mark.parametrize(
    ("choice", "stored"),
    [
        (ModelChoice(model="a:b"), "a:b"),
        (ModelChoice(thinking_level="off"), {"thinking_level": "off"}),
        (ModelChoice("a:b", "high"), {"model": "a:b", "thinking_level": "high"}),
    ],
)
def test_stored(choice, stored):
    assert choice.stored() == stored


def test_merge_model_choices_merges_per_field():
    merged = merge_model_choices(
        {"x": ModelChoice(model="a:b")}, {"x": ModelChoice(thinking_level="low"), "y": ModelChoice(model="c:d")}
    )
    assert merged == {"x": ModelChoice("a:b", "low"), "y": ModelChoice(model="c:d")}
