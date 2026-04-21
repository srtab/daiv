from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.http import require_POST
from django.views.generic import ListView, TemplateView

from notifications.channels.registry import all_channels
from notifications.channels.rocketchat import verify_username
from notifications.choices import ChannelType
from notifications.models import Notification, UserChannelBinding


class UserChannelsView(LoginRequiredMixin, TemplateView):
    template_name = "notifications/channels_page.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        bindings_by_type = {b.channel_type: b for b in UserChannelBinding.objects.filter(user=self.request.user)}
        rocketchat_error = self.request.session.pop("rocketchat_error", "")
        rocketchat_username_value = self.request.session.pop("rocketchat_username_value", "")
        rows = []
        for cls in all_channels():
            rows.append({
                "channel_type": cls.channel_type,
                "display_name": cls.display_name,
                "binding": bindings_by_type.get(cls.channel_type),
            })
        ctx["channel_rows"] = rows
        ctx["rocketchat_error"] = rocketchat_error
        ctx["rocketchat_username_value"] = rocketchat_username_value
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
        username = (request.POST.get("username") or "").strip().lstrip("@")
        redirect_url = reverse("user_channels")
        if not username:
            request.session["rocketchat_error"] = "Username is required."
            return HttpResponseRedirect(redirect_url)

        rc_user_id, error = verify_username(username)
        if error is not None or rc_user_id is None:
            request.session["rocketchat_error"] = error or "User not found."
            request.session["rocketchat_username_value"] = username
            return HttpResponseRedirect(redirect_url)

        UserChannelBinding.objects.update_or_create(
            user=request.user,
            channel_type=ChannelType.ROCKETCHAT,
            defaults={"address": username, "is_verified": True, "verified_at": timezone.now()},
        )
        return HttpResponseRedirect(redirect_url)


@method_decorator(require_POST, name="dispatch")
class DeleteRocketChatBindingView(LoginRequiredMixin, View):
    def post(self, request):
        UserChannelBinding.objects.filter(user=request.user, channel_type=ChannelType.ROCKETCHAT).delete()
        return HttpResponseRedirect(reverse("user_channels"))
