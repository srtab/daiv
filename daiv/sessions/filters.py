from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

import django_filters

from sessions.artifacts import KNOWN_KIND_CONTENT_TYPES, ArtifactKind, content_types_for_kind
from sessions.models import RunArtifact, RunStatus, Session, SessionOrigin

if TYPE_CHECKING:
    from accounts.models import User
    from sessions.managers import SessionQuerySet

RANGE_CHOICES: list[tuple[str, str]] = [
    ("today", _("Today")),
    ("2d", _("Last 2 days")),
    ("7d", _("Last 7 days")),
    ("30d", _("Last 30 days")),
]

STATUS_CHOICES: list[tuple[str, str]] = [
    (status.value, status.label)
    for status in (
        RunStatus.WAITING_INPUT,
        RunStatus.FAILED,
        RunStatus.RUNNING,
        RunStatus.QUEUED,
        RunStatus.READY,
        RunStatus.SUCCESSFUL,
    )
]

SCOPE_MINE = "mine"
SCOPE_ALL = "all"
SCOPE_CHOICES: list[tuple[str, str]] = [(SCOPE_MINE, _("Mine")), (SCOPE_ALL, _("Everyone"))]

DEEP_LINK_PARAMS = ("repo", "mr", "schedule", "batch")


def default_scope(data) -> str:
    """The scope a URL without a valid ``scope`` gets: ``all`` for a deep link, else ``mine``."""
    return SCOPE_ALL if any(data.get(param) for param in DEEP_LINK_PARAMS) else SCOPE_MINE


def resolve_scope(data) -> str:
    """The scope a request's query params select: an explicit valid ``scope``, else :func:`default_scope`."""
    scope = data.get("scope")
    return scope if scope in dict(SCOPE_CHOICES) else default_scope(data)


def scope_queryset(queryset: SessionQuerySet, user: User, scope: str) -> SessionQuerySet:
    """The sessions ``scope`` shows ``user``: the ones they own for ``mine``, all they may see for ``all``."""
    return queryset.owned_by(user) if scope == SCOPE_MINE else queryset.visible_to(user)


# Rolling-window size per range token; "today" is special-cased to local calendar midnight.
_RANGE_WINDOWS: dict[str, timedelta] = {"2d": timedelta(days=2), "7d": timedelta(days=7), "30d": timedelta(days=30)}


class SessionFilter(django_filters.FilterSet):
    # Param names match the old activity deep links (?trigger=, ?status=, ...).
    scope = django_filters.ChoiceFilter(choices=SCOPE_CHOICES, method="filter_scope")
    q = django_filters.CharFilter(method="filter_q")
    status = django_filters.ChoiceFilter(choices=STATUS_CHOICES, method="filter_status")
    trigger = django_filters.ChoiceFilter(field_name="origin", choices=SessionOrigin.choices)
    repo = django_filters.CharFilter(field_name="repo_id")  # deep-link back-compat: clearable chip, no widget to set it
    mr = django_filters.NumberFilter(method="filter_mr")
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

    def __init__(self, data=None, *args, **kwargs):
        data = data.copy() if data is not None else {}
        data["scope"] = resolve_scope(data)
        super().__init__(data, *args, **kwargs)

    def filter_scope(self, queryset, name, value):
        # The view applies the scope as its base queryset (``scope_queryset``); here it is only validated.
        return queryset

    def filter_q(self, queryset, name, value):
        return queryset.filter(Q(title__icontains=value) | Q(repo_id__icontains=value))

    def filter_status(self, queryset, name, value):
        # Requires the with_latest_status() annotation on the base queryset.
        return queryset.filter(latest_run_status=value)

    def filter_batch(self, queryset, name, value):
        return queryset.filter(runs__batch_id=value).distinct()

    def filter_mr(self, queryset, name, value):
        # IIDs are per-project, so this pools every repository's request of that number unless
        # ``repo`` is also given; the links DAIV publishes always pair the two.
        return queryset.for_merge_request(value)

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


class ArtifactFilter(django_filters.FilterSet):
    q = django_filters.CharFilter(method="filter_q")
    kind = django_filters.ChoiceFilter(choices=ArtifactKind.choices, method="filter_kind")
    repo = django_filters.CharFilter(field_name="run__session__repo_id")

    class Meta:
        model = RunArtifact
        fields: list[str] = []

    def filter_q(self, queryset, name, value):
        return queryset.filter(Q(title__icontains=value) | Q(filename__icontains=value))

    def filter_kind(self, queryset, name, value):
        if value == ArtifactKind.OTHER:
            return queryset.exclude(content_type__in=KNOWN_KIND_CONTENT_TYPES)
        return queryset.filter(content_type__in=content_types_for_kind(value))
