from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

import django_filters

from accounts.models import User
from mcp_servers.models import MCPServer


class MCPServerFilter(django_filters.FilterSet):
    """Owner picker for the personal MCP servers page.

    Admin-only: for members the ``owner`` filter is removed outright and the
    queryset is always pinned to their own rows, so a hand-crafted ``?owner=``
    never leaks another member's list. No selection ("You") or an invalid value
    means the requesting user's own servers.
    """

    owner = django_filters.ModelChoiceFilter(
        field_name="user",
        queryset=User.objects.filter(is_active=True).order_by("name"),
        empty_label=_("You"),
        widget=forms.Select,
    )

    class Meta:
        model = MCPServer
        # All filters are declared above; disable auto-generation from model fields.
        fields: list[str] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user = getattr(self.request, "user", None)
        self.for_admin = bool(user is not None and user.is_authenticated and user.is_admin)
        if not self.for_admin:
            # Popping before ``.form`` is first accessed (it's lazy) removes the
            # form field too, so members never even render the dropdown.
            self.filters.pop("owner", None)

    @property
    def qs(self):
        base = super().qs
        owner = None
        if self.for_admin and self.is_bound and self.form.is_valid():
            owner = self.form.cleaned_data.get("owner")
        if owner is None:
            # Default (and the member/invalid-value fallback): your own servers.
            base = base.filter(user=self.request.user)
        return base
