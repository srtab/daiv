"""Shared form fields for any surface that submits an agent run.

The prompt-box UI emits a single ``repos`` hidden input containing a JSON
list of ``{repo_id, ref}`` entries. :class:`RepoListField` parses and validates
it so ``cleaned_data["repos"]`` is a list of dicts; the caller converts to
``RepoTarget`` and hands off to :func:`sessions.services.submit_batch_runs`.
"""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from notifications.choices import NotifyOn
from sandbox_envs.models import SandboxEnvironment

from automation.agent.validators import AgentOverrideError, ensure_agent_model_available, validate_agent_override
from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE, RepositoryAccessDenied, assert_can_run
from core.models import ThinkingLevelChoices
from sessions.services import validate_repo_list


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


class AgentRunFieldsMixin(forms.Form):
    prompt = forms.CharField(label=_("Prompt"), required=True)
    repos = RepoListField(required=True)
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

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        if "sandbox_environment" in self.fields and user is not None:
            self.fields["sandbox_environment"].queryset = SandboxEnvironment.objects.visible_to(user)

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
        return cleaned


class AgentRunCreateForm(AgentRunFieldsMixin, forms.Form):
    """Validate 'Start a run' submissions. Orchestration lives in ``sessions.services``."""
