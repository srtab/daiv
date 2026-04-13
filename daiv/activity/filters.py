from __future__ import annotations

import django_filters

from activity.models import Activity, ActivityStatus, TriggerType


class ActivityFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=ActivityStatus.choices)
    trigger = django_filters.ChoiceFilter(field_name="trigger_type", choices=TriggerType.choices)
    repo = django_filters.CharFilter(field_name="repo_id")
    schedule = django_filters.NumberFilter(field_name="scheduled_job_id")
    date_from = django_filters.DateFilter(field_name="created_at", lookup_expr="date__gte")
    date_to = django_filters.DateFilter(field_name="created_at", lookup_expr="date__lte")

    class Meta:
        model = Activity
        # All filters are declared above; disable auto-generation from model fields.
        fields: list[str] = []
