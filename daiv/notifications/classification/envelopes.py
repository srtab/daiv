"""The ``actionable[]`` item contract for :class:`notifications.models.RunEnvelope`.

This is a **pure** module: it must never import from ``notifications.models`` (the model
imports the validator from here), keeping the contract a standalone source of truth that
the classifier (Story 1.3) and every reader build/read against.
"""

from __future__ import annotations

from typing import TypedDict

from django.core.exceptions import ValidationError

# Bump when the item shape changes so stored payloads remain interpretable.
ACTIONABLE_SCHEMA_VERSION = 1


class ActionableItem(TypedDict):
    """One thing a user can act on within a classified run.

    No filterable/queryable state (e.g. ``status``) may live inside the payload.
    """

    id: str
    kind: str
    label: str
    ref: str
    schema_version: int


REQUIRED_ACTIONABLE_KEYS = frozenset({"id", "kind", "label", "ref", "schema_version"})
# Filterable/queryable state must be a first-class column, never buried in the JSON.
FORBIDDEN_ACTIONABLE_KEYS = frozenset({"status"})


def validate_actionable(items: list) -> None:
    """Validate an ``actionable[]`` payload against the item contract.

    Each item must be a mapping carrying every key in ``REQUIRED_ACTIONABLE_KEYS``, none in
    ``FORBIDDEN_ACTIONABLE_KEYS``, contract-correct value types (``id``/``kind``/``label``/
    ``ref`` are ``str``, ``schema_version`` is ``int``), and a unique ``id`` within the list.
    Other keys are ignored: stored rows may carry keys outside the contract.

    Raises:
        django.core.exceptions.ValidationError: if any item breaks the contract. This is the
            single boundary guard for raw/classifier-authored payloads, so every malformed
            input surfaces here as a field-scoped ``ValidationError`` (never a bare
            ``TypeError`` from, e.g., an unhashable ``id``).
    """
    if not isinstance(items, list):
        raise ValidationError({"actionable": "actionable must be a list."})

    seen_ids: set = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValidationError({"actionable": f"actionable[{index}] must be a mapping."})

        if missing := REQUIRED_ACTIONABLE_KEYS - item.keys():
            raise ValidationError({"actionable": f"actionable[{index}] is missing required keys: {sorted(missing)}."})

        if forbidden := FORBIDDEN_ACTIONABLE_KEYS & item.keys():
            raise ValidationError({"actionable": f"actionable[{index}] contains forbidden keys: {sorted(forbidden)}."})

        for key in ("id", "kind", "label", "ref"):
            if not isinstance(item[key], str):
                raise ValidationError({"actionable": f"actionable[{index}].{key} must be a string."})
        # ``bool`` is an ``int`` subclass; a boolean is not a valid schema_version.
        if not isinstance(item["schema_version"], int) or isinstance(item["schema_version"], bool):
            raise ValidationError({"actionable": f"actionable[{index}].schema_version must be an int."})

        item_id = item["id"]
        if item_id in seen_ids:
            raise ValidationError({"actionable": f"actionable[{index}] has a duplicate id: {item_id!r}."})
        seen_ids.add(item_id)


def build_actionable_item(
    *,
    # ``id`` deliberately shadows the builtin: it is the item's contract key name.
    id: str,  # noqa: A002
    kind: str,
    label: str,
    ref: str,
) -> ActionableItem:
    """Build a contract-shaped ``ActionableItem``, stamping the current schema version.

    The single constructor every producer (the classifier and any consumer) uses, so no
    one hand-builds items or forgets ``schema_version``.
    """
    return {"id": id, "kind": kind, "label": label, "ref": ref, "schema_version": ACTIONABLE_SCHEMA_VERSION}
