from __future__ import annotations

import django_filters

from memory.models import MemoryObservation, ObservationCategory, ObservationStatus


class MemoryObservationFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=ObservationStatus.choices)
    category = django_filters.ChoiceFilter(choices=ObservationCategory.choices)

    class Meta:
        model = MemoryObservation
        # All filters are declared above; disable auto-generation from model fields.
        fields: list[str] = []
