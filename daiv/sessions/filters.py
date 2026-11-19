from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

import django_filters

from sessions.artifacts import KNOWN_KIND_CONTENT_TYPES, ArtifactKind, content_types_for_kind
from sessions.models import Run, RunArtifact, RunStatus, Session, SessionOrigin, SpendRecord, SpendTask

if TYPE_CHECKING:
    from django.db.models import QuerySet

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

ARCHIVED_ONLY = "true"
ARCHIVED_INCLUDED = "all"

SPEND_PARAMS = (
    "spend_from",
    "spend_to",
    "spend_person",
    "spend_repo",
    "spend_trigger",
    "spend_task",
    "spend_model",
    "spend_provider",
)
DEEP_LINK_PARAMS = ("repo", "mr", "schedule", "batch", *SPEND_PARAMS)

SORT_RECENT = "recent"
SORT_COST = "cost"
SORT_CHOICES: list[tuple[str, str]] = [(SORT_RECENT, _("Most recent")), (SORT_COST, _("Cost"))]


def resolve_scope(data) -> str:
    """The scope a request's query params select: an explicit valid ``scope``, else ``all`` for a deep link."""
    scope = data.get("scope")
    if scope in dict(SCOPE_CHOICES):
        return scope
    return SCOPE_ALL if any(data.get(param) for param in DEEP_LINK_PARAMS) else SCOPE_MINE


# Rolling-window size per range token; "today" is special-cased to local calendar midnight.
_RANGE_WINDOWS: dict[str, timedelta] = {"2d": timedelta(days=2), "7d": timedelta(days=7), "30d": timedelta(days=30)}


def local_midnight(day: date) -> datetime:
    # A bound on the raw column rather than a ``__date`` cast, so the last_active_at index applies.
    return timezone.make_aware(datetime.combine(day, time.min))


def validate_person_key(value: str) -> None:
    from sessions.spend_ledger import SpendLedger

    try:
        SpendLedger.person_filter(value)
    except ValueError as err:
        raise ValidationError(_("Not a person key."), code="invalid") from err


class SessionFilter(django_filters.FilterSet):
    # Param names match the old activity deep links (?trigger=, ?status=, ...).
    q = django_filters.CharFilter(method="filter_q")
    status = django_filters.ChoiceFilter(choices=STATUS_CHOICES, method="filter_status")
    trigger = django_filters.ChoiceFilter(field_name="origin", choices=SessionOrigin.choices)
    repo = django_filters.CharFilter(method="filter_repo")  # deep-link back-compat: clearable chip, no widget to set it
    mr = django_filters.NumberFilter(method="filter_mr")
    schedule = django_filters.NumberFilter(field_name="scheduled_job_id")
    batch = django_filters.UUIDFilter(method="filter_batch")
    range = django_filters.ChoiceFilter(choices=RANGE_CHOICES, method="filter_range")
    # Dates filter last_active_at (the column the list sorts on and every row shows).
    date_from = django_filters.DateFilter(method="filter_date_from")
    date_to = django_filters.DateFilter(method="filter_date_to")
    ran_on = django_filters.DateFilter(method="filter_ran_on")
    # The spend params combine into one match on a single record, in ``filter_queryset``.
    spend_from = django_filters.DateFilter(method="keep")
    spend_to = django_filters.DateFilter(method="keep")
    spend_person = django_filters.CharFilter(method="keep", validators=[validate_person_key])
    spend_repo = django_filters.CharFilter(method="keep")
    spend_trigger = django_filters.ChoiceFilter(choices=SessionOrigin.choices, method="keep")
    spend_task = django_filters.ChoiceFilter(choices=SpendTask.choices, method="keep")
    spend_model = django_filters.CharFilter(method="keep")
    spend_provider = django_filters.CharFilter(method="keep")
    sort = django_filters.ChoiceFilter(choices=SORT_CHOICES, method="keep")

    class Meta:
        model = Session
        # All filters are declared above; disable auto-generation from model fields.
        fields: list[str] = []

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        records = self.spend_records(self.form.cleaned_data)
        if records is None:
            return queryset
        return queryset.filter(Exists(records.filter(session=OuterRef("pk"))))

    @staticmethod
    def spend_records(cleaned: dict) -> QuerySet[SpendRecord] | None:
        """The records every given spend param matches, or None with none given: a session matches when one record
        matches them all, so a period and a model never pair one run's period with another run's model."""
        from sessions.spend_ledger import SpendLedger

        lookups = {
            "spend_repo": "repo_id",
            "spend_trigger": "trigger_type",
            "spend_task": "task",
            "spend_model": "model",
            "spend_provider": "provider_slug",
        }
        if not any(cleaned.get(name) for name in SPEND_PARAMS):
            return None
        q = Q(**{field: cleaned[name] for name, field in lookups.items() if cleaned.get(name)})
        if cleaned.get("spend_person"):
            q &= SpendLedger.person_filter(cleaned["spend_person"])
        return SpendRecord.objects.filter(q).between(cleaned.get("spend_from"), cleaned.get("spend_to"))

    def keep(self, queryset, name, value):
        return queryset

    def filter_q(self, queryset, name, value):
        return queryset.filter(Q(title__icontains=value) | Q(repo_id__icontains=value))

    def filter_status(self, queryset, name, value):
        # Requires the with_latest_status() annotation on the base queryset.
        return queryset.filter(latest_run_status=value)

    def filter_batch(self, queryset, name, value):
        return queryset.filter(runs__batch_id=value).distinct()

    def filter_repo(self, queryset, name, value):
        # With ``mr`` too, filter_mr matches the request on any of a session's repositories, not just its main one.
        if self.form.cleaned_data.get("mr") is not None:
            return queryset
        return queryset.filter(repo_id=value)

    def filter_mr(self, queryset, name, value):
        # IIDs are per-project, so this pools every repository's request of that number unless
        # ``repo`` is also given; the links DAIV publishes always pair the two.
        return queryset.on_merge_request(value, repo_id=self.form.cleaned_data.get("repo") or None)

    def filter_date_from(self, queryset, name, value):
        return queryset.filter(last_active_at__gte=local_midnight(value))

    def filter_date_to(self, queryset, name, value):
        return queryset.filter(last_active_at__lt=local_midnight(value + timedelta(days=1)))

    def filter_ran_on(self, queryset, name, value):
        start = local_midnight(value)
        ran = Run.objects.filter(created_at__gte=start, created_at__lt=local_midnight(value + timedelta(days=1)))
        return queryset.filter(pk__in=ran.values("session_id"))

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
