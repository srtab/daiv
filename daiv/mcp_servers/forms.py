from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from mcp_servers.models import MCPServer


class MCPServerForm(forms.ModelForm):
    """Create/edit a custom MCP server.

    Headers are managed via a separate formset (added in Task 12). On edit,
    ``name`` is immutable (added in Task 13). Tool filter items can come
    either as a list of strings (when checkbox UI is used) or as a
    newline-separated textarea (the offline fallback).
    """

    tool_filter_items = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": _("one tool name per line")}),
        help_text=_("One tool name per line."),
    )

    class Meta:
        model = MCPServer
        fields = ("name", "description", "transport", "url", "enabled", "tool_filter_mode")
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, discovered_tools=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.discovered_tools = discovered_tools  # consumed in Task 14
        if self.instance.pk is not None:
            self.fields["tool_filter_items"].initial = "\n".join(self.instance.tool_filter_items or [])

    def clean_tool_filter_items(self):
        raw = self.cleaned_data.get("tool_filter_items") or ""
        items = [line.strip() for line in raw.splitlines() if line.strip()]
        return items

    def clean(self):
        cleaned = super().clean()
        if (
            cleaned.get("tool_filter_mode")
            and cleaned["tool_filter_mode"] != MCPServer.FilterMode.NONE
            and not cleaned.get("tool_filter_items")
        ):
            self.add_error(
                "tool_filter_items", _("At least one item is required when a filter mode other than 'none' is set.")
            )
        return cleaned

    def save(self, commit: bool = True) -> MCPServer:
        self.instance.tool_filter_items = self.cleaned_data.get("tool_filter_items", [])
        return super().save(commit=commit)
