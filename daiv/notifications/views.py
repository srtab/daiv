from __future__ import annotations

from typing import NamedTuple
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.http import require_POST
from django.views.generic import ListView, TemplateView

from core.site_settings import site_settings
from notifications.channels.registry import enabled_channels
from notifications.channels.rocketchat import RocketChatChannel
from notifications.channels.telegram import TelegramChannel
from notifications.choices import ChannelType
from notifications.forms import RocketChatBindingForm
from notifications.models import Notification, UserChannelBinding
from notifications.telegram.tokens import mint_token
from notifications.telegram_bindings import binding_state, unbind_user


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
                "connect_ready": self._connect_ready(cls.channel_type),
            })
        ctx["channel_rows"] = rows
        return ctx

    @staticmethod
    def _connect_ready(channel_type: str) -> bool:
        """Telegram's deep link needs a derived bot username; without one the link is dead."""
        if channel_type == ChannelType.TELEGRAM:
            return bool(site_settings.telegram_bot_username)
        return True


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
        return ctx


class BellDropdownView(LoginRequiredMixin, TemplateView):
    template_name = "notifications/_bell_dropdown.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        qs = Notification.objects.filter(recipient=self.request.user).prefetch_related("deliveries")
        # Fetch before the bulk-update below so unread cues still render on first open.
        ctx["notifications"] = list(qs[:10])
        Notification.mark_all_read_for(self.request.user)
        return ctx


@method_decorator(require_POST, name="dispatch")
class MarkNotificationReadView(LoginRequiredMixin, TemplateView):
    template_name = "notifications/_notification_row.html"

    def post(self, request, notification_id):
        notification = get_object_or_404(Notification, id=notification_id, recipient=request.user)
        notification.mark_as_read()
        return self.render_to_response({"notification": notification})


@method_decorator(require_POST, name="dispatch")
class MarkAllReadView(LoginRequiredMixin, View):
    def post(self, request):
        Notification.mark_all_read_for(request.user)
        return HttpResponseRedirect(reverse("notifications:list"))


@method_decorator(require_POST, name="dispatch")
class UpdateRocketChatBindingView(LoginRequiredMixin, View):
    def post(self, request):
        if not RocketChatChannel.is_enabled():
            raise Http404
        redirect_url = reverse("user_channels")
        form = RocketChatBindingForm(request.POST)
        if not form.is_valid():
            for errors in form.errors.values():
                for msg in errors:
                    messages.error(request, msg)
            return HttpResponseRedirect(redirect_url)

        UserChannelBinding.objects.update_or_create(
            user=request.user,
            channel_type=ChannelType.ROCKETCHAT,
            defaults={"address": form.cleaned_data["username"], "is_verified": True, "verified_at": timezone.now()},
        )
        return HttpResponseRedirect(redirect_url)


@method_decorator(require_POST, name="dispatch")
class DeleteRocketChatBindingView(LoginRequiredMixin, View):
    def post(self, request):
        UserChannelBinding.objects.filter(user=request.user, channel_type=ChannelType.ROCKETCHAT).delete()
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
        address, verified_at = binding_state(request.user)
        token = mint_token(request.user.pk, address=address, verified_at=verified_at)
        return HttpResponseRedirect(f"https://t.me/{quote(bot_username, safe='')}?start={token}")


@method_decorator(require_POST, name="dispatch")
class DisconnectTelegramView(LoginRequiredMixin, View):
    def post(self, request):
        unbind_user(request.user)
        return HttpResponseRedirect(reverse("user_channels"))
