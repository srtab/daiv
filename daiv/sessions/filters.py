from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

import django_filters

from sessions.models import RunStatus, Session, SessionOrigin

RANGE_CHOICES: list[tuple[str, str]] = [
    ("today", _("Today")),
    ("2d", _("Last 2 days")),
    ("7d", _("Last 7 days")),
    ("30d", _("Last 30 days")),
]

# Rolling-window size per range token; "today" is special-cased to local calendar midnight.
_RANGE_WINDOWS: dict[str, timedelta] = {"2d": timedelta(days=2), "7d": timedelta(days=7), "30d": timedelta(days=30)}


class SessionFilter(django_filters.FilterSet):
    # Param names match the old activity deep links (?trigger=, ?status=, ...).
    q = django_filters.CharFilter(method="filter_q")
    status = django_filters.ChoiceFilter(choices=RunStatus.choices, method="filter_status")
    trigger = django_filters.ChoiceFilter(field_name="origin", choices=SessionOrigin.choices)
    repo = django_filters.CharFilter(field_name="repo_id")  # deep-link back-compat: clearable chip, no widget to set it
    schedule = django_filters.NumberFilter(field_name="scheduled_job_id")
    batch = django_filters.UUIDFilter(method="filter_batch")
    range = django_filters.ChoiceFilter(choices=RANGE_CHOICES, method="filter_range")
    # Dates filter last_active_at (the column the list sorts on and every row shows).
    date_from = django_filters.DateFilter(field_name="last_active_at", lookup_expr="date__gte")
    date_to = django_filters.DateFilter(field_name="last_active_at", lookup_expr="date__lte")

    class Meta:
        model = Session
        # All filters are declared above; disable auto-generation from model fields.
        fields: list[str] = []

    def filter_q(self, queryset, name, value):
        return queryset.filter(Q(title__icontains=value) | Q(repo_id__icontains=value))

    def filter_status(self, queryset, name, value):
        # Requires the with_latest_status() annotation on the base queryset.
        return queryset.filter(latest_run_status=value)

    def filter_batch(self, queryset, name, value):
        return queryset.filter(runs__batch_id=value).distinct()

    def filter_range(self, queryset, name, value):
        # Rolling windows; "today" is the local calendar day. The first-party UI never emits
        # range together with date_from/date_to; a hand-built URL sending both will AND them
        # (nothing enforces mutual exclusivity server-side).
        now = timezone.now()
        if value == "today":
            start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
        elif value in _RANGE_WINDOWS:
            start = now - _RANGE_WINDOWS[value]
        else:
            return queryset
        return queryset.filter(last_active_at__gte=start)
