"""Shared form fields for any surface that submits an agent run.

The prompt-box UI emits a single ``repos`` hidden input containing a JSON
list of ``{repo_id, ref}`` entries. :class:`RepoListField` parses and validates
it so ``cleaned_data["repos"]`` is a list of dicts; the caller converts to
``RepoTarget`` and hands off to :func:`activity.services.submit_batch_runs`.
"""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from notifications.choices import NotifyOn
from sandbox_envs.models import SandboxEnvironment
from sandbox_envs.services import visible_envs_for

from activity.services import validate_repo_list


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
    use_max = forms.BooleanField(
        label=_("Use max model"),
        required=False,
        initial=False,
        help_text=_("More capable model with thinking set to high."),
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
        if "sandbox_environment" in self.fields and user is not None:
            self.fields["sandbox_environment"].queryset = visible_envs_for(user)


class AgentRunCreateForm(AgentRunFieldsMixin, forms.Form):
    """Validate 'Start a run' submissions. Orchestration lives in ``activity.services``."""
