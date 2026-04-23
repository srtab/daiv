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

from activity.services import validate_repo_list


class RepoListField(forms.JSONField):
    """Form field for a JSON-encoded ``[{"repo_id", "ref"}, ...]`` hidden input."""

    widget = forms.HiddenInput
    default_error_messages = {"invalid": _("Malformed repository list.")}

    def to_python(self, value):
        parsed = super().to_python(value)
        if parsed is None:
            return None
        try:
            return validate_repo_list(parsed)
        except ValueError as err:
            raise forms.ValidationError(str(err)) from err

    def prepare_value(self, value):
        # Widget value is embedded verbatim into Alpine's initialRepos; empty must serialize as "[]", not "null".
        if value is None:
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


class AgentRunCreateForm(AgentRunFieldsMixin, forms.Form):
    """Validate 'Start a run' submissions. Orchestration lives in ``activity.services``."""
