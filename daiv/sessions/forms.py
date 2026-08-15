"""Shared form fields for any surface that submits an agent run.

The prompt-box UI emits a single ``repos`` hidden input containing a JSON
list of ``{repo_id, ref}`` entries. :class:`RepoListField` parses and validates
it so ``cleaned_data["repos"]`` is a list of dicts; the caller converts to
``RepoTarget`` and hands off to :func:`sessions.services.submit_batch_runs`.
"""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from mcp_servers.selection import build_selection_pool, diff_selection, effective_selection, parse_server_names
from notifications.choices import NotifyOn
from sandbox_envs.models import SandboxEnvironment

from automation.agent.validators import AgentOverrideError, ensure_agent_model_available, validate_agent_override
from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE, RepositoryAccessDenied, assert_can_run
from core.models import ThinkingLevelChoices
from sessions.validators import validate_repo_list


class RepoListField(forms.JSONField):
    """Form field for a JSON-encoded ``[{"repo_id", "ref"}, ...]`` hidden input.

    With ``required=False`` an *exactly-empty* list bypasses ``validate_repo_list``
    (which enforces a 1-entry minimum) — used by schedule templates where empty
    means "let users choose". Other falsy or malformed shapes still fall through
    to validation so the user sees an explicit error rather than a silent reset.
    """

    widget = forms.HiddenInput
    default_error_messages = {"invalid": _("Malformed repository list.")}

    def to_python(self, value):
        parsed = super().to_python(value)
        if parsed is None:
            return None
        if parsed == [] and not self.required:
            return []
        try:
            return validate_repo_list(parsed)
        except ValueError as err:
            raise forms.ValidationError(str(err)) from err

    def prepare_value(self, value):
        # Widget value is embedded verbatim into Alpine's initialRepos; empty must serialize as "[]", not "null".
        if value in (None, []):
            return "[]"
        return super().prepare_value(value)


class MCPSelectionField(forms.JSONField):
    """Hidden JSON list of checked MCP server names. ``required=False``; ``to_python`` normalizes
    None/empty to ``[]`` and rejects non-list-of-str. ``prepare_value`` returns the list itself (not
    a JSON string): the Alpine picker seeds ``x-model`` from ``mcp_picker_context`` — which reads
    ``BoundField.value()`` and expects a list — and overwrites the hidden input's DOM ``value`` on
    init, so no JSON-string serialization is needed here."""

    widget = forms.HiddenInput
    default_error_messages = {"invalid": _("Malformed MCP selection.")}

    def to_python(self, value):
        parsed = super().to_python(value)
        # ``None`` ("nothing was submitted") must stay distinguishable from ``[]`` ("the user
        # turned every server off"): collapsing them lets an unpopulated input — Alpine
        # blocked, a scripted POST, a form that doesn't render the picker — read as an
        # explicit deselect-all and silently disable every default server. ``clean()`` is
        # what acts on the difference.
        if parsed in (None, ""):
            return None
        try:
            return parse_server_names(parsed)
        except ValueError as err:
            raise forms.ValidationError(self.error_messages["invalid"], code="invalid") from err

    def prepare_value(self, value):
        if isinstance(value, str):
            # Bound-form re-render: BoundField.value() feeds the raw submitted string back through
            # prepare_value; parse it so callers always see a list, degrading a malformed value to [].
            try:
                return self.to_python(value) or []
            except forms.ValidationError:
                return []
        return list(value) if value else []


class AgentRunFieldsMixin(forms.Form):
    prompt = forms.CharField(label=_("Prompt"), required=True)
    repos = RepoListField(required=True)
    mcp_servers = MCPSelectionField(required=False)
    agent_model = forms.CharField(
        label=_("Agent model"),
        required=False,
        empty_value="",
        help_text=_("Override the configured model for this run."),
    )
    agent_thinking_level = forms.ChoiceField(
        label=_("Thinking effort"), choices=[("", "")] + list(ThinkingLevelChoices.choices), required=False
    )
    notify_on = forms.ChoiceField(label=_("Notify me"), choices=NotifyOn.choices, required=True)
    sandbox_environment = forms.ModelChoiceField(
        # Queryset is scoped to the caller in ``__init__``; an empty default avoids
        # leaking other users' USER-scoped envs if a subclass forgets to pass ``user``.
        queryset=SandboxEnvironment.objects.none(),
        required=False,
        empty_label=_("(global default)"),
        label=_("Sandbox environment"),
    )

    def __init__(self, *args, user=None, owner=None, mcp_overrides=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        if "sandbox_environment" in self.fields and user is not None:
            self.fields["sandbox_environment"].queryset = SandboxEnvironment.objects.visible_to(user)

        # Pool is scoped to the run/schedule OWNER, not the editing admin, so an admin
        # editing a member's schedule sees the member's USER-scoped servers.
        self.mcp_owner = owner or user
        self.mcp_pool = build_selection_pool(getattr(self.mcp_owner, "pk", None))
        if "mcp_servers" in self.fields and not self.is_bound:
            # ``mcp_overrides`` is for the instance-less forms (a retry prefills from the source
            # run's session); ``ModelForm`` subclasses fall back to the row being edited.
            stored = mcp_overrides or getattr(getattr(self, "instance", None), "mcp_overrides", {}) or {}
            self.fields["mcp_servers"].initial = sorted(effective_selection(stored, self.mcp_pool))

    def clean(self):
        cleaned = super().clean() or {}
        try:
            cleaned["agent_model"], cleaned["agent_thinking_level"] = validate_agent_override(
                cleaned.get("agent_model"), cleaned.get("agent_thinking_level")
            )
            # Server-side backstop for the picker's HTML5 ``required`` — if the
            # client-side gate is bypassed (curl, scripted submit, a stale page
            # cached when a system default still existed), surface the same error
            # as a form error instead of letting the run enqueue and explode at
            # ``get_daiv_agent_kwargs`` time.
            ensure_agent_model_available(cleaned["agent_model"])
        except AgentOverrideError as err:
            self.add_error("agent_model", str(err))

        repos = cleaned.get("repos") or []
        if self.user is not None and repos:
            try:
                assert_can_run(self.user, [entry["repo_id"] for entry in repos])
            except RepositoryAccessDenied:
                self.add_error("repos", REPO_ACCESS_DENIED_MESSAGE)

        # Absent means "leave the stored selection alone", matching the chat endpoint
        # (``chat/api/views.py``); only a selection that was actually submitted is diffed.
        # An empty list is a real answer and still diffs to "everything off".
        submitted = cleaned.get("mcp_servers")
        if submitted is not None:
            cleaned["mcp_overrides"] = diff_selection(set(submitted), self.mcp_pool)
        else:
            cleaned["mcp_overrides"] = getattr(getattr(self, "instance", None), "mcp_overrides", None) or {}
        return cleaned


class AgentRunCreateForm(AgentRunFieldsMixin, forms.Form):
    """Validate 'Start a run' submissions. Orchestration lives in ``sessions.services``."""
