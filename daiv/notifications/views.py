from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_POST
from django.views.generic import ListView, TemplateView

from notifications.channels.registry import all_channels
from notifications.models import Notification, UserChannelBinding


class UserChannelsView(LoginRequiredMixin, TemplateView):
    template_name = "notifications/channels_page.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        bindings_by_type = {b.channel_type: b for b in UserChannelBinding.objects.filter(user=self.request.user)}
        rows = []
        for cls in all_channels():
            rows.append({
                "channel_type": cls.channel_type,
                "display_name": cls.display_name,
                "binding": bindings_by_type.get(cls.channel_type),
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
        ctx["unread_count"] = Notification.objects.filter(recipient=self.request.user, read_at__isnull=True).count()
        return ctx


@method_decorator(require_POST, name="dispatch")
class MarkNotificationReadView(LoginRequiredMixin, TemplateView):
    template_name = "notifications/_notification_row.html"

    def post(self, request, notification_id):
        notification = get_object_or_404(Notification, id=notification_id, recipient=request.user)
        if notification.read_at is None:
            notification.read_at = timezone.now()
            notification.save(update_fields=["read_at"])
        return self.render_to_response({"notification": notification})


@method_decorator(require_POST, name="dispatch")
class MarkAllReadView(LoginRequiredMixin, TemplateView):
    template_name = "notifications/notification_list.html"

    def post(self, request):
        Notification.objects.filter(recipient=request.user, read_at__isnull=True).update(read_at=timezone.now())
        return HttpResponse(status=204)
