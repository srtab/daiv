from __future__ import annotations

import django_filters

from sessions.models import RunStatus, Session, SessionOrigin


class SessionFilter(django_filters.FilterSet):
    # Param names match the old activity deep links (?trigger=, ?status=, ...).
    status = django_filters.ChoiceFilter(choices=RunStatus.choices, method="filter_status")
    trigger = django_filters.ChoiceFilter(field_name="origin", choices=SessionOrigin.choices)
    repo = django_filters.CharFilter(field_name="repo_id")
    schedule = django_filters.NumberFilter(field_name="scheduled_job_id")
    batch = django_filters.UUIDFilter(method="filter_batch")
    date_from = django_filters.DateFilter(field_name="created_at", lookup_expr="date__gte")
    date_to = django_filters.DateFilter(field_name="created_at", lookup_expr="date__lte")

    class Meta:
        model = Session
        # All filters are declared above; disable auto-generation from model fields.
        fields: list[str] = []

    def filter_status(self, queryset, name, value):
        # Requires the with_latest_status() annotation on the base queryset.
        return queryset.filter(latest_run_status=value)

    def filter_batch(self, queryset, name, value):
        return queryset.filter(runs__batch_id=value).distinct()
