from __future__ import annotations

import re

from django import forms
from django.utils.translation import gettext_lazy as _

from mcp_servers.models import MCPServer

# RFC 7230 token grammar for header names.
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$")


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
        self.discovered_tools = discovered_tools
        if self.instance.pk is not None:
            self.fields["tool_filter_items"].initial = "\n".join(self.instance.tool_filter_items or [])

        # Dynamic field swap: when the view passes ``discovered_tools``,
        # render a checkbox list of those tools (plus any persisted-but-
        # not-discovered tools, flagged) instead of the free textarea.
        if discovered_tools:
            discovered_names = [t.get("name") for t in discovered_tools if t.get("name")]
            persisted = list(self.instance.tool_filter_items or []) if self.instance.pk is not None else []
            extra = [n for n in persisted if n not in discovered_names]

            choices: list[tuple[str, str]] = []
            for t in discovered_tools:
                if not t.get("name"):
                    continue
                desc = t.get("description") or ""
                label = f"{t['name']} — {desc}" if desc else t["name"]
                choices.append((t["name"], label))
            for n in extra:
                choices.append((n, _("%(name)s (not in current tool list)") % {"name": n}))

            self.fields["tool_filter_items"] = forms.MultipleChoiceField(
                choices=choices, widget=forms.CheckboxSelectMultiple, required=False, initial=persisted
            )

    def clean_name(self):
        name = self.cleaned_data["name"]
        if self.instance.pk is not None and name != self.instance.name:
            raise forms.ValidationError(
                _("Renaming an existing MCP server is not supported. Delete and re-create instead.")
            )
        return name

    def clean_tool_filter_items(self):
        raw = self.cleaned_data.get("tool_filter_items") or []
        if isinstance(raw, str):
            return [line.strip() for line in raw.splitlines() if line.strip()]
        return list(raw)

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


class MCPServerHeaderForm(forms.Form):
    name = forms.CharField(max_length=255)
    mode = forms.ChoiceField(choices=[("literal", "literal"), ("env_ref", "env_ref")])
    value = forms.CharField(required=False, max_length=4096)

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError(_("Header name cannot be empty."))
        if not _HEADER_NAME_RE.match(name):
            raise forms.ValidationError(_("Invalid header name (RFC 7230 token characters only)."))
        return name

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("mode")
        value = (cleaned.get("value") or "").strip()
        # An env_ref with an empty value is invalid (no var name). A literal
        # with an empty value is valid: it means "preserve existing on edit"
        # (handled by build_headers_from_formset). On create, the existing
        # is None and the resulting empty literal is dropped from the list.
        if mode == "env_ref" and not value:
            self.add_error("value", _("Environment variable name is required."))
        return cleaned


MCPServerHeaderFormSet = forms.formset_factory(MCPServerHeaderForm, extra=0, can_delete=True, max_num=50)


def build_headers_from_formset(formset, *, existing: list[dict] | None) -> list[dict]:
    """Build the model's ``headers`` list from a validated formset.

    For literal entries, a blank submitted value means "preserve the
    existing value at this header name" (matching ``sandbox_envs``'s
    secret-preserving form behavior). Entries marked for deletion in the
    formset are dropped.
    """
    existing_by_name = {h["name"]: h for h in (existing or [])}
    out: list[dict] = []
    for form in formset.forms:
        if not form.cleaned_data:
            continue
        if form.cleaned_data.get("DELETE"):
            continue
        name = form.cleaned_data["name"]
        mode = form.cleaned_data["mode"]
        value = (form.cleaned_data.get("value") or "").strip()
        if mode == "literal" and not value and name in existing_by_name:
            out.append(existing_by_name[name])
            continue
        if mode == "literal" and not value:
            # Empty literal on create — skip (nothing to persist).
            continue
        out.append({"name": name, "mode": mode, "value": value})
    return out
