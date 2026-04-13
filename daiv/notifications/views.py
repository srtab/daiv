from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView

from notifications.channels.registry import all_channels
from notifications.models import UserChannelBinding


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
