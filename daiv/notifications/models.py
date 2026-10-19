import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from django_extensions.db.models import TimeStampedModel

from core import ui_events
from notifications.choices import ChannelType, DeliveryStatus, EnvelopeStatus, EventType
from notifications.classification.envelopes import validate_actionable


def publish_unread_changed(user_id) -> None:
    """Poke the recipient's nav readers to recompute their unread count.

    Deferred to commit so the reader's recount cannot run against a snapshot that
    predates the write; outside a transaction ``on_commit`` fires straight away.
    Called from every site that changes unread state — the model methods below and
    ``services.create_notification`` — rather than from the views on top of them, so
    a new caller can't silently leave the badge stale.
    """
    transaction.on_commit(lambda: ui_events.publisher.notifications_changed(user_id))


class Notification(TimeStampedModel):
    """An in-app notification addressed to a single user.

    Each notification may have one or more NotificationDelivery rows,
    one per channel through which delivery was attempted.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications", verbose_name=_("recipient")
    )
    event_type = models.CharField(_("event type"), max_length=64, choices=EventType.choices)
    source_type = models.CharField(_("source type"), max_length=64, blank=True, default="")
    source_id = models.CharField(_("source id"), max_length=64, blank=True, default="")
    subject = models.CharField(_("subject"), max_length=255)
    body = models.TextField(_("body"))
    link_url = models.CharField(_("link URL"), max_length=500, blank=True, default="")
    # DjangoJSONEncoder (not the stock encoder) so payload builders can put
    # gettext_lazy proxies, datetimes, Decimals, and UUIDs in the context without each
    # call site remembering to coerce — the alternative was a silent save failure.
    context = models.JSONField(_("context"), default=dict, blank=True, encoder=DjangoJSONEncoder)
    read_at = models.DateTimeField(_("read at"), null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["recipient", "read_at"], name="notif_recipient_read_idx"),
            models.Index(fields=["recipient", "-created"], name="notif_recipient_created_idx"),
        ]
        constraints = [
            # Exactly-once per (recipient, source, event_type) across the per-run and batch events:
            # racing emits and the re-drive backstop all elect one DB winner; the loser is swallowed.
            models.UniqueConstraint(
                fields=["recipient", "source_type", "source_id", "event_type"],
                condition=models.Q(
                    event_type__in=[
                        EventType.JOB_BATCH_FINISHED,
                        EventType.JOB_FINISHED,
                        EventType.SCHEDULE_FINISHED,
                        EventType.PIPELINE_WATCH_EXHAUSTED,
                        EventType.SCHEDULE_DISPATCH_FAILED,
                    ]
                ),
                name="notif_unique_per_source_recipient",
            )
        ]
        ordering = ["-created"]

    def __str__(self) -> str:
        return f"{self.event_type} → {self.recipient_id}"

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    def mark_as_read(self) -> None:
        """Mark this notification as read, idempotently."""
        if self.read_at is None:
            self.read_at = timezone.now()
            self.save(update_fields=["read_at", "modified"])
            publish_unread_changed(self.recipient_id)

    @classmethod
    def mark_all_read_for(cls, user) -> int:
        now = timezone.now()
        marked = cls.objects.filter(recipient=user, read_at__isnull=True).update(read_at=now, modified=now)
        if marked:
            publish_unread_changed(user.pk)
        return marked


class NotificationDelivery(TimeStampedModel):
    """Tracks the delivery of a notification through a specific channel.

    At most one delivery per (notification, channel_type) pair.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    notification = models.ForeignKey(
        Notification, on_delete=models.CASCADE, related_name="deliveries", verbose_name=_("notification")
    )
    channel_type = models.CharField(_("channel type"), max_length=32, choices=ChannelType.choices)
    address = models.CharField(_("address"), max_length=255)
    status = models.CharField(
        _("status"), max_length=16, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING
    )
    attempts = models.PositiveIntegerField(_("attempts"), default=0)
    last_attempted_at = models.DateTimeField(_("last attempted at"), null=True, blank=True)
    delivered_at = models.DateTimeField(_("delivered at"), null=True, blank=True)
    error_message = models.TextField(_("error message"), blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["notification", "channel_type"], name="notif_delivery_unique_channel"),
            models.CheckConstraint(
                condition=~models.Q(status=DeliveryStatus.SENT) | models.Q(delivered_at__isnull=False),
                name="notif_delivery_sent_has_delivered_at",
            ),
        ]
        indexes = [models.Index(fields=["status", "last_attempted_at"], name="notif_delivery_status_idx")]

    def __str__(self) -> str:
        return f"{self.channel_type}:{self.status}"

    def mark_sent(self) -> None:
        """Record a successful delivery."""
        self.status = DeliveryStatus.SENT
        self.delivered_at = timezone.now()
        self.error_message = ""
        self.save(update_fields=["status", "delivered_at", "error_message", "modified"])

    def mark_failed(self, error: str) -> None:
        """Record a permanent or max-attempts failure."""
        self.status = DeliveryStatus.FAILED
        self.error_message = error
        self.save(update_fields=["status", "error_message", "modified"])

    def mark_skipped(self, reason: str) -> None:
        """Record that delivery was skipped (unknown channel, no binding, etc.)."""
        self.status = DeliveryStatus.SKIPPED
        self.error_message = reason
        self.save(update_fields=["status", "error_message", "modified"])


class UserChannelBinding(TimeStampedModel):
    """Maps a user to a verified delivery address on a notification channel.

    Used during notification creation to resolve the recipient address
    for each requested channel.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="channel_bindings", verbose_name=_("user")
    )
    channel_type = models.CharField(_("channel type"), max_length=32, choices=ChannelType.choices)
    address = models.CharField(_("address"), max_length=255)
    extra_config = models.JSONField(_("extra config"), default=dict, blank=True)
    is_verified = models.BooleanField(_("verified"), default=False)
    verified_at = models.DateTimeField(_("verified at"), null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "channel_type", "address"], name="user_channel_binding_unique"),
            models.CheckConstraint(
                condition=models.Q(is_verified=False) | models.Q(verified_at__isnull=False),
                name="user_channel_binding_verified_has_timestamp",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.channel_type}:{self.address}"


class RunEnvelope(models.Model):
    """The structured classification of a completed ``Run``.

    The sole store of a run's classification (``status`` / ``count`` / ``summary`` /
    ``actionable[]``), OneToOne to its run. The ``run.envelope`` reverse accessor raises
    ``DoesNotExist`` while the run is still classifying. It is **not** a throughput/merge
    record — that is ``codebase.MergeMetric`` (AD-10), which this model never touches.
    """

    run = models.OneToOneField(
        "agent_sessions.Run", on_delete=models.CASCADE, related_name="envelope", verbose_name=_("run")
    )
    # No default: the classifier (Story 1.3) always sets a status; an unset value is invalid.
    status = models.CharField(_("status"), max_length=16, choices=EnvelopeStatus.choices, db_index=True)
    count = models.PositiveIntegerField(_("count"), default=0)
    # The classifier's one-line gloss — distinct from ``Run.result_summary`` (the full
    # agent-response fallback); do not conflate the two.
    summary = models.TextField(_("summary"), blank=True, default="")
    # The house list-JSON idiom (cf. ``codebase.topics``). The item contract lives in
    # ``notifications.classification.envelopes`` (enforced by ``clean()``); never store filterable state inside.
    actionable = models.JSONField(_("actionable"), default=list, blank=True)
    # Mirrors ``Run.created_at``; supports Feed freshness/ordering and leaves room for a later
    # composite ``["status", "-created_at"]`` index without a rewrite.
    created_at = models.DateTimeField(_("created at"), default=timezone.now, editable=False)

    class Meta:
        # Renaming the table would rewrite it along with its index and constraint names.
        db_table = "agent_sessions_runenvelope"
        verbose_name = _("Run Envelope")
        verbose_name_plural = _("Run Envelopes")
        # ``-id`` tiebreaker keeps Feed ordering/pagination deterministic when envelopes
        # share a ``created_at`` (cf. RunManager's ("-created_at", "-id") ordering).
        ordering = ["-created_at", "-id"]
        constraints = [
            # DB-level enum enforcement (``choices=`` alone is not enforced, and
            # ``.aupdate()``/raw writes bypass field validation). References
            # ``EnvelopeStatus.values`` directly so the constraint cannot drift.
            models.CheckConstraint(
                condition=models.Q(status__in=EnvelopeStatus.values), name="run_envelope_status_valid"
            )
        ]

    def __str__(self) -> str:
        return f"Envelope({self.status}) for run {self.run_id}"

    def save(self, *args, **kwargs):
        """Keep ``count`` a derived mirror of ``len(actionable)``.

        ``count`` is a queryable column (the Feed badge) but never an independently-authored value:
        deriving it here means no writer — an admin edit, a data fix, a future second producer — can
        persist a count that disagrees with the list. (The classifier task also sets it explicitly;
        this makes the coherence structural rather than convention.)
        """
        self.count = len(self.actionable)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = set(update_fields)
            if "actionable" in update_fields:
                update_fields.add("count")
                kwargs["update_fields"] = update_fields
        super().save(*args, **kwargs)

    def clean(self) -> None:
        """Enforce the ``actionable[]`` contract and the FR-5 status<->actionable coherence invariant.

        Defense-in-depth at the model boundary: the Story 1.3 classifier already guarantees these,
        but ``full_clean()`` should independently reject an incoherent envelope so an admin edit, a
        raw ``RunEnvelope(...)`` build, or a future second writer cannot persist one. Both directions
        of the found-issues invariant are enforced here; ``count`` is kept coherent in ``save()``.
        """
        super().clean()
        validate_actionable(self.actionable)
        if self.status == EnvelopeStatus.FOUND_ISSUES and not self.actionable:
            raise ValidationError({"actionable": "A found-issues envelope must list at least one actionable item."})
        if self.status != EnvelopeStatus.FOUND_ISSUES and self.actionable:
            raise ValidationError({"actionable": "Only a found-issues envelope may carry actionable items."})
