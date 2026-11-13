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
        ("", EMPTY_CHOICE),
        (None, EMPTY_CHOICE),
    ],
)
def test_parse_model_choice(value, expected):
    assert parse_model_choice(value, source="test") == expected


def test_an_unknown_level_is_dropped_and_logged_by_source_keeping_the_model(caplog):
    with caplog.at_level(logging.ERROR):
        choice = parse_model_choice({"model": "x:y", "thinking_level": "extreme"}, source=".daiv.yml")
    assert choice == ModelChoice(model="x:y")
    assert ".daiv.yml" in caplog.text
    assert "extreme" not in caplog.text


def test_parse_model_choices_never_logs_entry_names_or_values(caplog):
    with caplog.at_level(logging.ERROR):
        parse_model_choices({"secret-name": {"model": 42, "thinking_level": "s3cr3t"}, 7: "x:y"}, source="ENV_VAR")
    assert "ENV_VAR" in caplog.text
    assert not {"secret-name", "42", "s3cr3t", "7"} & set(caplog.text.replace(":", " ").split())


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


def test_a_boolean_false_level_is_off_because_yaml_reads_an_unquoted_off_that_way():
    assert parse_model_choice({"thinking_level": False}, source="test") == ModelChoice(thinking_level="off")


@pytest.mark.parametrize("level", ["False", "false", " FALSE "])
def test_a_stringified_false_level_is_off_because_deepagents_stringifies_metadata(level):
    assert parse_model_choice({"thinking_level": level}, source="test") == ModelChoice(thinking_level="off")


def test_a_map_entry_with_an_unknown_key_is_logged_by_source_only(caplog):
    with caplog.at_level(logging.ERROR):
        choices = parse_model_choices({"cr-tests": {"thinking": "xhigh"}}, source=".daiv.yml")

    assert choices == {}
    assert "unknown keys" in caplog.text
    assert ".daiv.yml" in caplog.text
    assert "cr-tests" not in caplog.text
    assert "xhigh" not in caplog.text


def test_a_single_choice_reads_its_keys_from_a_wider_mapping_silently(caplog):
    with caplog.at_level(logging.DEBUG):
        choice = parse_model_choice({"name": "cr-tests", "model": "a:b", "thinking_level": "low"}, source="charter")

    assert choice == ModelChoice("a:b", "low")
    assert not caplog.records
