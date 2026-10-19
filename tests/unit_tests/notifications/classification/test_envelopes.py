from django.core.exceptions import ValidationError

import pytest
from notifications.classification.envelopes import ACTIONABLE_SCHEMA_VERSION, build_actionable_item, validate_actionable


def _item(item_id: str = "f1", **overrides) -> dict:
    item = build_actionable_item(id=item_id, kind="finding", label="Fix the bug", ref="app/module.py:10")
    item.update(overrides)
    return item


def test_validate_actionable_accepts_well_formed_list():
    validate_actionable([_item("a"), _item("b")])  # no raise


def test_validate_actionable_rejects_non_list():
    with pytest.raises(ValidationError):
        validate_actionable({"id": "x"})


def test_validate_actionable_rejects_non_dict_item():
    with pytest.raises(ValidationError):
        validate_actionable(["not-a-mapping"])


def test_validate_actionable_rejects_missing_required_key():
    item = _item()
    del item["ref"]
    with pytest.raises(ValidationError):
        validate_actionable([item])


def test_validate_actionable_rejects_forbidden_status_key():
    with pytest.raises(ValidationError):
        validate_actionable([_item(status="found-issues")])


def test_validate_actionable_rejects_duplicate_id():
    with pytest.raises(ValidationError):
        validate_actionable([_item("dup"), _item("dup")])


def test_build_actionable_item_stamps_schema_version():
    item = build_actionable_item(id="f1", kind="finding", label="L", ref="r")
    assert item == {
        "id": "f1",
        "kind": "finding",
        "label": "L",
        "ref": "r",
        "schema_version": ACTIONABLE_SCHEMA_VERSION,
    }
    assert item["schema_version"] == 1


def test_validate_actionable_ignores_a_legacy_fix_prompt_key():
    validate_actionable([_item(fix_prompt="patch it")])  # no raise


def test_validate_actionable_rejects_non_string_id_without_typeerror():
    """An unhashable/non-string id must surface as ValidationError, never a bare TypeError."""
    with pytest.raises(ValidationError):
        validate_actionable([_item(id=["not", "a", "string"])])


@pytest.mark.parametrize(
    "bad", [{"kind": 1}, {"label": None}, {"ref": []}, {"schema_version": "1"}, {"schema_version": True}]
)
def test_validate_actionable_rejects_wrong_value_types(bad):
    with pytest.raises(ValidationError):
        validate_actionable([_item(**bad)])
