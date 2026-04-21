from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.http import require_POST
from django.views.generic import ListView, TemplateView

from notifications.channels.registry import enabled_channels
from notifications.channels.rocketchat import RocketChatChannel
from notifications.choices import ChannelType
from notifications.forms import RocketChatBindingForm
from notifications.models import Notification, UserChannelBinding

_CHANNEL_CONNECT_URLS = {
    ChannelType.ROCKETCHAT: ("notifications:rocketchat_connect", "notifications:rocketchat_disconnect", "@username")
}


class UserChannelsView(LoginRequiredMixin, TemplateView):
    template_name = "notifications/channels_page.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        bindings_by_type = {b.channel_type: b for b in UserChannelBinding.objects.filter(user=self.request.user)}
        rows = []
        for cls in enabled_channels():
            connect = _CHANNEL_CONNECT_URLS.get(cls.channel_type)
            row = {
                "channel_type": cls.channel_type,
                "display_name": cls.display_name,
                "binding": bindings_by_type.get(cls.channel_type),
                "connect_url": reverse(connect[0]) if connect else "",
                "disconnect_url": reverse(connect[1]) if connect else "",
                "connect_placeholder": connect[2] if connect else "",
            }
            rows.append(row)
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
        return ctx


class BellDropdownView(LoginRequiredMixin, TemplateView):
    template_name = "notifications/_bell_dropdown.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        qs = Notification.objects.filter(recipient=self.request.user).prefetch_related("deliveries")
        ctx["notifications"] = list(qs[:10])
        return ctx


class BellBadgeView(LoginRequiredMixin, TemplateView):
    template_name = "notifications/_bell.html"


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
        Notification.objects.filter(recipient=request.user, read_at__isnull=True).update(read_at=timezone.now())
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
