from __future__ import annotations

from types import SimpleNamespace

from notifications.channels.renderers.events import EVENT_ROWS, event_rows
from notifications.choices import EventType


class TestRegistryLookup:
    def test_every_event_type_has_rows(self):
        # A missing definition is not a hard failure: each channel degrades to plain text and logs a
        # warning per delivery. Nothing else stops a new event shipping without a card.
        assert [e.value for e in EventType if e not in EVENT_ROWS] == []

    def test_lookup_works_with_bare_string_value(self):
        # Callers receive notification.event_type as a string off the CharField; the registry
        # is keyed by EventType but must resolve via the underlying str (TextChoices is-a str).
        assert event_rows(SimpleNamespace(event_type="job.finished", context={})) is not None

    def test_unknown_event_type_returns_none(self):
        assert event_rows(SimpleNamespace(event_type="does.not.exist", context={})) is None
