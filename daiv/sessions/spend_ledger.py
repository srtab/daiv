"""Reading ``SpendRecord`` rows: who a row's person is, the rows a user may export, and the API's paging cursor.

A person is keyed ``u<user id>``, ``x<provider>:<uid>`` for a git platform account, ``d<former user id>`` for a
deleted user (shown by the name stamped as they were deleted) or ``daiv`` for work no person started. External
accounts are matched by platform and account id, never by username, which another person may share.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from django.db.models import Q
from django.utils.translation import gettext as _

from sessions.api.schemas import SpendRowSchema
from sessions.models import RunStatus, SpendRecord

if TYPE_CHECKING:
    from datetime import date
    from decimal import Decimal

    from django.db.models import QuerySet

    from accounts.models import User

DAIV_PERSON = "daiv"
DAIV_LABEL = "DAIV"

COLUMNS = tuple(SpendRowSchema.model_fields)

PersonKind = Literal["user", "external", "deleted", "daiv"]


@dataclass(frozen=True)
class Person:
    key: str
    name: str
    kind: PersonKind


class SpendLedger:
    """The person keys, the row source behind the CSV export and the API, and the API's cursor."""

    PERSON_VALUES = (
        "user_id",
        "user__username",
        "user__name",
        "external_provider",
        "external_uid",
        "external_username",
        "deleted_user_id",
        "person_name",
    )

    @staticmethod
    def person(values: dict[str, Any] | SpendRecord) -> Person:
        """The person of a record, or of a ``values()`` row holding ``PERSON_VALUES``."""
        get = values.get if isinstance(values, dict) else lambda name: _record_value(values, name)
        if get("user_id") is not None:
            return Person(f"u{get('user_id')}", get("user__name") or get("user__username") or "", "user")
        if get("external_provider") and get("external_uid"):
            key = f"x{get('external_provider')}:{get('external_uid')}"
            return Person(key, get("external_username") or get("external_uid") or "", "external")
        if get("deleted_user_id") is not None:
            return Person(f"d{get('deleted_user_id')}", get("person_name") or _("Unknown person"), "deleted")
        return Person(DAIV_PERSON, DAIV_LABEL, "daiv")

    @staticmethod
    def person_filter(key: str) -> Q:
        """The records of the person ``key``; ``ValueError`` when it is no person key."""
        if key == DAIV_PERSON:
            return Q(user__isnull=True, deleted_user_id__isnull=True) & (Q(external_provider="") | Q(external_uid=""))
        kind, rest = key[:1], key[1:]
        if kind == "u" and rest.isdigit():
            return Q(user_id=int(rest))
        if kind == "x" and ":" in rest:
            provider, uid = rest.split(":", 1)
            if provider and uid:
                return Q(user__isnull=True, external_provider=provider, external_uid=uid)
        if kind == "d" and rest.removeprefix("-").isdigit():
            return Q(user__isnull=True, deleted_user_id=int(rest)) & (Q(external_provider="") | Q(external_uid=""))
        raise ValueError(f"Not a person key: {key!r}")

    @staticmethod
    def rows(user: User, scope: str, start: date | None, end: date) -> QuerySet[SpendRecord]:
        """The records ``user`` may see from local midnight of ``start`` (or ever) to the end of ``end``, oldest first;
        under ``scope="mine"`` only those they started. SYNC ONLY, as ``SpendRecord.objects.visible_to`` is."""
        records = SpendRecord.objects.for_scope(user, scope).between(start, end)
        return records.select_related("user", "session", "run").only(*_ROW_FIELDS).order_by("occurred_at", "id")

    @classmethod
    def row(cls, record: SpendRecord) -> dict[str, Any]:
        """One record as the export and the API serve it, keyed by ``COLUMNS``, with None for an empty value."""
        person = cls.person(record)
        if person.kind == "user":
            person_text = record.user.username
        elif person.kind == "daiv":
            person_text = DAIV_LABEL
        else:
            person_text = person.name
        return {
            "occurred_at": _utc(record.occurred_at),
            "kind": "background" if record.task else "run",
            "task": record.task or None,
            "trigger_type": record.trigger_type or None,
            "person": person_text,
            "person_kind": person.kind,
            "repo_id": record.repo_id or None,
            "session_id": record.session_id,
            "run_id": str(record.run_id) if record.run_id else None,
            "schedule_id": record.session.scheduled_job_id if record.session_id else None,
            "model": record.model,
            "provider": record.provider_slug or None,
            "provider_name": record.provider_name or None,
            "input_tokens": record.input_tokens,
            "cache_read_tokens": record.cache_read_tokens,
            "cache_write_tokens": record.cache_write_tokens,
            "output_tokens": record.output_tokens,
            "reasoning_tokens": record.reasoning_tokens,
            "total_tokens": record.total_tokens,
            "cost_usd": _money(record.cost_usd),
            "reported_cost_usd": _money(record.reported_cost_usd) if record.reported_cost_usd is not None else None,
            "unpriced_tokens": record.unpriced_tokens,
            "cache_reported": record.cache_reported,
            "cache_saving_usd": _money(record.cache_saving_usd),
            "cache_unpriced_tokens": record.cache_unpriced_tokens,
            "cost_source": cls.cost_source(record),
            "final": bool(record.task) or record.run_id is None or record.run.status in RunStatus.terminal(),
            "updated_at": _utc(record.updated_at),
        }

    @staticmethod
    def cost_source(record: SpendRecord) -> str:
        """How the record's cost was worked out: ``reported``, ``estimated``, ``mixed``, ``unknown`` or ``unpriced``."""
        if not record.cost_usd and record.unpriced_tokens and not record.cost_reported:
            return "unpriced"
        if record.reported_cost_usd is None:
            return "unknown"
        sources = {
            "reported": record.cost_reported,
            "estimated": record.cost_usd > record.reported_cost_usd,
            "unpriced": record.unpriced_tokens > 0,
        }
        present = [source for source, found in sources.items() if found]
        if len(present) > 1:
            return "mixed"
        return present[0] if present else "estimated"

    @staticmethod
    def encode_cursor(occurred_at: datetime, pk: int) -> str:
        payload = json.dumps({"t": occurred_at.isoformat(), "i": pk}, separators=(",", ":"))
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    @staticmethod
    def decode_cursor(text: str) -> tuple[datetime, int]:
        """``(occurred_at, id)`` from :meth:`encode_cursor`; ``ValueError`` when ``text`` is not one."""
        try:
            payload = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
            occurred_at, pk = datetime.fromisoformat(payload["t"]), payload["i"]
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as err:
            raise ValueError("Malformed cursor") from err
        if not isinstance(pk, int) or isinstance(pk, bool) or occurred_at.tzinfo is None:
            raise ValueError("Malformed cursor")
        return occurred_at, pk


_ROW_FIELDS = (
    *(field.name for field in SpendRecord._meta.concrete_fields),
    "user__username",
    "user__name",
    "session__scheduled_job",
    "run__status",
)


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _money(value: Decimal) -> str:
    return f"{value:.6f}"


def _record_value(record: SpendRecord, name: str) -> Any:
    if name.startswith("user__"):
        return getattr(record.user, name.removeprefix("user__"), None) if record.user_id is not None else None
    return getattr(record, name)
