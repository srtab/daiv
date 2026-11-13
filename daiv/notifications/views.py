from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import NamedTuple
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.http import require_POST
from django.views.generic import ListView, TemplateView

from core.site_settings import site_settings
from core.utils import build_absolute_url
from notifications.channels.discord import DiscordChannel, DiscordPermanentError, DiscordTransientError, _DiscordClient
from notifications.channels.registry import enabled_channels
from notifications.channels.rocketchat import RocketChatChannel
from notifications.channels.telegram import TelegramChannel
from notifications.choices import ChannelType
from notifications.forms import DiscordBindingForm, RocketChatBindingForm
from notifications.models import Notification, UserChannelBinding
from notifications.outcomes import attach_outcomes
from notifications.telegram.tokens import mint_token
from notifications.telegram_bindings import binding_state_for_pk

logger = logging.getLogger("daiv.notifications")
_DISCORD_CHALLENGE_TTL_SECONDS = 600


class ChannelConnect(NamedTuple):
    """One channel's connect-UI descriptor — named so the positional read can't drift.

    ``style`` is what the template branches on: ``"input"`` renders the username text field
    every other connectable channel uses, ``"link"`` renders a single button that redirects to
    an external handshake.
    """

    connect_url_name: str
    disconnect_url_name: str
    placeholder: str = ""
    style: str = "input"


_CHANNEL_CONNECT = {
    ChannelType.ROCKETCHAT: ChannelConnect(
        "notifications:rocketchat_connect", "notifications:rocketchat_disconnect", "@username"
    ),
    ChannelType.DISCORD: ChannelConnect(
        "notifications:discord_connect", "notifications:discord_disconnect", "User ID (123456789012345678)"
    ),
    ChannelType.TELEGRAM: ChannelConnect(
        "notifications:telegram_connect", "notifications:telegram_disconnect", style="link"
    ),
}


class UserChannelsView(LoginRequiredMixin, TemplateView):
    template_name = "notifications/channels_page.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        bindings_by_type = {b.channel_type: b for b in UserChannelBinding.objects.filter(user=self.request.user)}
        rows = []
        for cls in enabled_channels():
            connect = _CHANNEL_CONNECT.get(cls.channel_type)
            rows.append({
                "channel_type": cls.channel_type,
                "display_name": cls.display_name,
                "binding": bindings_by_type.get(cls.channel_type),
                "connect_url": reverse(connect.connect_url_name) if connect else "",
                "disconnect_url": reverse(connect.disconnect_url_name) if connect else "",
                "connect_placeholder": connect.placeholder if connect else "",
                "connect_style": connect.style if connect else "",
                "connect_ready": cls.connect_ready(),
                "connect_blocked_reason": cls.connect_blocked_reason(),
            })
        ctx["channel_rows"] = rows
        return ctx


class NotificationListView(LoginRequiredMixin, ListView):
    template_name = "notifications/notification_list.html"
    context_object_name = "notifications"
    paginate_by = 20

    def get_queryset(self):
        qs = Notification.objects.filter(recipient=self.request.user).prefetch_related("deliveries")
        status = self.request.GET.get("status")
        if status == "unread":
            qs = qs.filter(read_at__isnull=True)
        elif status == "read":
            qs = qs.filter(read_at__isnull=False)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["current_filter"] = self.request.GET.get("status", "all")
        attach_outcomes(ctx["notifications"])
        return ctx


@method_decorator(require_POST, name="dispatch")
class MarkNotificationReadView(LoginRequiredMixin, View):
    def post(self, request, notification_id):
        notification = get_object_or_404(Notification, id=notification_id, recipient=request.user)
        notification.mark_as_read()
        return HttpResponse(status=204)


@method_decorator(require_POST, name="dispatch")
class MarkAllReadView(LoginRequiredMixin, View):
    def post(self, request):
        Notification.mark_all_read_for(request.user)
        return HttpResponseRedirect(reverse("notifications:list"))


@method_decorator(require_POST, name="dispatch")
class UpdateChannelBindingView(LoginRequiredMixin, View):
    channel_class = None
    channel_type = None
    form_class = None

    def get_form(self, request):
        return self.form_class(request.POST)

    def perform_binding(self, request, form) -> None:
        UserChannelBinding.objects.update_or_create(
            user=request.user,
            channel_type=self.channel_type,
            defaults={"address": form.cleaned_data["username"], "is_verified": True, "verified_at": timezone.now()},
        )

    def post(self, request):
        if not self.channel_class.is_enabled():
            raise Http404
        redirect_url = reverse("user_channels")
        form = self.get_form(request)
        if not form.is_valid():
            for errors in form.errors.values():
                for msg in errors:
                    messages.error(request, msg)
            return HttpResponseRedirect(redirect_url)
        self.perform_binding(request, form)
        return HttpResponseRedirect(redirect_url)


class UpdateRocketChatBindingView(UpdateChannelBindingView):
    channel_class = RocketChatChannel
    channel_type = ChannelType.ROCKETCHAT
    form_class = RocketChatBindingForm


class UpdateDiscordBindingView(UpdateChannelBindingView):
    channel_class = DiscordChannel
    channel_type = ChannelType.DISCORD
    form_class = DiscordBindingForm

    def get_form(self, request):
        return self.form_class(request.POST, user=request.user)

    def perform_binding(self, request, form) -> None:
        client = _DiscordClient.from_site_settings()
        if client is None:
            messages.error(request, _("Discord is not configured."))
            return
        user_id = form.cleaned_data["username"]
        token = secrets.token_urlsafe(32)
        confirmation_url = build_absolute_url(reverse("notifications:discord_confirm", args=[token]))
        try:
            client.send_direct_message(
                user_id,
                {
                    "content": _("Confirm this Discord account for DAIV notifications within 10 minutes: %(url)s")
                    % {"url": confirmation_url}
                },
            )
        except (DiscordPermanentError, DiscordTransientError) as exc:
            logger.warning("Discord ownership challenge for user %s could not be delivered: %s", request.user.pk, exc)
            messages.error(
                request, _("Discord could not send the confirmation message. Check the shared server and try again.")
            )
            return
        UserChannelBinding.objects.update_or_create(
            user=request.user,
            channel_type=ChannelType.DISCORD,
            defaults={
                "address": user_id,
                "is_verified": False,
                "verified_at": None,
                "extra_config": {
                    "challenge_hash": _discord_challenge_hash(token),
                    "challenge_expires_at": timezone.now().timestamp() + _DISCORD_CHALLENGE_TTL_SECONDS,
                },
            },
        )
        messages.success(request, _("Check your Discord direct messages for a confirmation link."))


def _discord_challenge_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ConfirmDiscordBindingView(LoginRequiredMixin, View):
    def get(self, request, token):
        if not DiscordChannel.is_enabled():
            raise Http404
        redirect_url = reverse("user_channels")
        challenge_hash = _discord_challenge_hash(token)
        with transaction.atomic():
            binding = (
                UserChannelBinding.objects
                .select_for_update()
                .filter(user=request.user, channel_type=ChannelType.DISCORD, is_verified=False)
                .first()
            )
            config = binding.extra_config if binding is not None else {}
            expires_at = config.get("challenge_expires_at")
            valid = (
                binding is not None
                and isinstance(expires_at, int | float)
                and not isinstance(expires_at, bool)
                and expires_at > timezone.now().timestamp()
                and hmac.compare_digest(str(config.get("challenge_hash", "")), challenge_hash)
            )
            if not valid:
                messages.error(request, _("This Discord confirmation link is invalid, expired, or already used."))
                return HttpResponseRedirect(redirect_url)
            binding.is_verified = True
            binding.verified_at = timezone.now()
            binding.extra_config = {}
            binding.save(update_fields=["is_verified", "verified_at", "extra_config", "modified"])
        messages.success(request, _("Discord connected. Your account is verified."))
        return HttpResponseRedirect(redirect_url)


@method_decorator(require_POST, name="dispatch")
class ChannelDisconnectView(LoginRequiredMixin, View):
    """Drop the caller's binding for one channel.

    ``channel_type`` is supplied per URL via ``as_view``, which only accepts keywords that are
    already class attributes — hence the assignment rather than a bare annotation.
    """

    channel_type: ChannelType | None = None

    def post(self, request):
        UserChannelBinding.objects.filter(user=request.user, channel_type=self.channel_type).delete()
        return HttpResponseRedirect(reverse("user_channels"))


@method_decorator(require_POST, name="dispatch")
class ConnectTelegramView(LoginRequiredMixin, View):
    """Mint a link token and hand the user off to the bot.

    Verification is inherent to the handshake — the user demonstrably messages the bot — so
    there is no ``verify_username`` equivalent and nothing is written here.
    """

    def post(self, request):
        if not TelegramChannel.is_enabled():
            raise Http404
        bot_username = site_settings.telegram_bot_username
        if not bot_username:
            messages.error(
                request, _("Telegram is not fully set up yet. Ask an administrator to finish the configuration.")
            )
            return HttpResponseRedirect(reverse("user_channels"))
        address, verified_at = binding_state_for_pk(request.user.pk)
        token = mint_token(request.user.pk, address=address, verified_at=verified_at)
        return HttpResponseRedirect(f"https://t.me/{quote(bot_username, safe='')}?start={token}")
