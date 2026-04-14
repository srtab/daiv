import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from notifications.choices import ChannelType, DeliveryStatus


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications", verbose_name=_("recipient")
    )
    event_type = models.CharField(_("event type"), max_length=64)
    source_type = models.CharField(_("source type"), max_length=64, blank=True, default="")
    source_id = models.CharField(_("source id"), max_length=64, blank=True, default="")
    subject = models.CharField(_("subject"), max_length=255)
    body = models.TextField(_("body"))
    link_url = models.CharField(_("link URL"), max_length=500, blank=True, default="")
    context = models.JSONField(_("context"), default=dict, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    read_at = models.DateTimeField(_("read at"), null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["recipient", "read_at"], name="notif_recipient_read_idx"),
            models.Index(fields=["recipient", "-created_at"], name="notif_recipient_created_idx"),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.event_type} → {self.recipient_id}"

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


class NotificationDelivery(models.Model):
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
            models.UniqueConstraint(fields=["notification", "channel_type"], name="notif_delivery_unique_channel")
        ]
        indexes = [models.Index(fields=["status", "last_attempted_at"], name="notif_delivery_status_idx")]

    def __str__(self) -> str:
        return f"{self.channel_type}:{self.status}"


class UserChannelBinding(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="channel_bindings", verbose_name=_("user")
    )
    channel_type = models.CharField(_("channel type"), max_length=32, choices=ChannelType.choices)
    address = models.CharField(_("address"), max_length=255)
    extra_config = models.JSONField(_("extra config"), default=dict, blank=True)
    is_verified = models.BooleanField(_("verified"), default=False)
    verified_at = models.DateTimeField(_("verified at"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "channel_type", "address"], name="user_channel_binding_unique")
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.channel_type}:{self.address}"
