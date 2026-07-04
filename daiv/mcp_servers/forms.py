from __future__ import annotations

import re

from django import forms
from django.utils.translation import gettext_lazy as _

from mcp_servers.constants import RESERVED_MCP_NAMES
from mcp_servers.models import MCPServer

# RFC 7230 token grammar for header names.
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$")


class MultiValueTextarea(forms.Textarea):
    """Textarea that also accepts multiple submitted values under the same name.

    The form's tool-filter field renders either as this textarea or as a
    checkbox list (server-side discovery, or the client-side Test-connection
    swap). Both POST shapes must validate identically, so this widget reads
    ``getlist()`` when available and hands the field a str or a list.
    """

    def value_from_datadict(self, data, files, name):
        if hasattr(data, "getlist"):
            values = data.getlist(name)
            if len(values) != 1:
                return values
            return values[0]
        return data.get(name)

    def format_value(self, value):
        if isinstance(value, (list, tuple)):
            value = "\n".join(value)
        return super().format_value(value)


class ToolFilterItemsField(forms.Field):
    """Normalises a newline-joined string or a list of values to ``list[str]``.

    No choice validation on purpose: arbitrary tool names are legal by design
    (the free-text path accepts them) and unknown names in an allow-list fail
    closed at runtime.
    """

    def to_python(self, value):
        if not value:
            return []
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
        return [item for item in (str(v).strip() for v in value) if item]


def build_tool_choices(discovered_tools, selected):
    """Build tool-filter checkbox rows for the template.

    ``discovered_tools`` is the list returned by discovery (dicts with
    ``name``/``description``). ``selected`` is the currently-selected tool
    names. Returns ``[]`` when nothing was discovered — the caller then renders
    the textarea fail-safe so a transient discovery failure can't wipe a
    persisted allow/block filter. Otherwise: discovered tools first, then any
    selected names not in the discovered list (``available=False``) so a
    renamed/removed tool stays un-checkable.
    """
    if not discovered_tools:
        return []
    selected = list(selected or [])
    selected_set = set(selected)
    rows: list[dict] = []
    seen: set[str] = set()
    for tool in discovered_tools:
        name = tool.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        rows.append({
            "value": name,
            "name": name,
            "description": tool.get("description") or "",
            "checked": name in selected_set,
            "available": True,
        })
    for name in selected:
        if name and name not in seen:
            seen.add(name)
            rows.append({"value": name, "name": name, "description": "", "checked": True, "available": False})
    return rows


class MCPServerForm(forms.ModelForm):
    """Create/edit a custom MCP server.

    Tool-filter items always use a getlist-aware textarea widget for data
    handling; the discovered-tool checkbox list is rendered by the template
    from ``build_tool_choices`` context, not by swapping this field's widget.
    On edit, ``name`` is immutable (read-only + ``clean_name`` guard).
    """

    tool_filter_items = ToolFilterItemsField(
        required=False,
        widget=MultiValueTextarea(attrs={"rows": 4, "placeholder": _("one tool name per line")}),
        help_text=_("One tool name per line."),
    )

    class Meta:
        model = MCPServer
        fields = ("name", "description", "transport", "url", "enabled", "tool_filter_mode")
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk is not None:
            self.fields["tool_filter_items"].initial = list(self.instance.tool_filter_items or [])
            # Name is immutable after creation (enforced by clean_name). Make the
            # field visibly inert; readonly (not disabled) keeps it in the POST so
            # clean_name still rejects a crafted rename.
            self.fields["name"].widget.attrs["readonly"] = True

    def clean_name(self):
        name = self.cleaned_data["name"]
        if self.instance.pk is not None and name != self.instance.name:
            raise forms.ValidationError(
                _("Renaming an existing MCP server is not supported. Delete and re-create instead.")
            )
        if name in RESERVED_MCP_NAMES:
            raise forms.ValidationError(_("'%(name)s' is a reserved name and cannot be used.") % {"name": name})
        return name

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
    mode = forms.ChoiceField(choices=MCPServer.HeaderMode.choices)
    value = forms.CharField(required=False, max_length=4096)

    def has_changed(self):
        # A trailing blank row (user clicked "Add header" then left it empty) is
        # treated as unchanged so the formset skips it instead of failing
        # clean_name. ``mode`` is ignored here: its <select> always submits a
        # value, which would otherwise make every empty row look "changed".
        if self.empty_permitted and not any((self[field].value() or "").strip() for field in ("name", "value")):
            return False
        return super().has_changed()

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
        if mode == MCPServer.HeaderMode.ENV_REF and not value:
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
        if mode == MCPServer.HeaderMode.LITERAL and not value and name in existing_by_name:
            out.append(existing_by_name[name])
            continue
        if mode == MCPServer.HeaderMode.LITERAL and not value:
            # Empty literal on create — skip (nothing to persist).
            continue
        out.append({"name": name, "mode": mode, "value": value})
    return out
