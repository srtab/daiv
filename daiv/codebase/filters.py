from __future__ import annotations

from django.db.models import Q

import django_filters

from codebase.models import CrossProjectAccessRecord


class CrossProjectAccessRecordFilterSet(django_filters.FilterSet):
    """The four questions an auditor asks of the cross-project log: who acted, what they reached,
    how it ended, and when."""

    acting_user = django_filters.CharFilter(method="filter_acting_user", label="Acting user")
    target_repo_id = django_filters.CharFilter(lookup_expr="icontains", label="Target project")
    thread_id = django_filters.CharFilter(label="Thread")
    outcome = django_filters.ChoiceFilter(choices=CrossProjectAccessRecord.OUTCOME_CHOICES)
    occurred_after = django_filters.DateFilter(field_name="occurred_at", lookup_expr="date__gte")
    occurred_before = django_filters.DateFilter(field_name="occurred_at", lookup_expr="date__lte")

    class Meta:
        model = CrossProjectAccessRecord
        # All filters are declared above; disable auto-generation from model fields.
        fields: list[str] = []

    def filter_acting_user(self, queryset, name, value):
        value = (value or "").strip()
        if not value:
            return queryset
        # The snapshot label is searched too, not only the FK: a deleted account is exactly the
        # case the snapshot exists for, and its rows must stay findable by name.
        return queryset.filter(
            Q(acting_user_label__icontains=value)
            | Q(acting_user__email__icontains=value)
            | Q(acting_user__username__icontains=value)
        )
