"""Shared form fields for any surface that submits an agent run.

The prompt-box UI emits a single ``repos_json`` hidden input containing a JSON
list of ``{repo_id, ref}`` entries. The form parses it into
``cleaned_data["repos"]`` as a list of dicts; the caller converts to
``RepoTarget`` and hands off to :func:`activity.services.submit_batch_runs`.
"""

from __future__ import annotations

import json

from django import forms
from django.utils.translation import gettext_lazy as _

from notifications.choices import NotifyOn

from activity.services import validate_repo_list


class AgentRunFieldsMixin(forms.Form):
    prompt = forms.CharField(label=_("Prompt"), required=True)
    repos_json = forms.CharField(widget=forms.HiddenInput, required=True)
    use_max = forms.BooleanField(
        label=_("Use max model"),
        required=False,
        initial=False,
        help_text=_("More capable model with thinking set to high."),
    )
    notify_on = forms.ChoiceField(label=_("Notify me"), choices=NotifyOn.choices, required=True)

    def clean_repos_json(self) -> str:
        raw = (self.cleaned_data.get("repos_json") or "").strip()
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError as err:
            raise forms.ValidationError(_("Malformed repository list.")) from err

        try:
            self.cleaned_data["repos"] = validate_repo_list(parsed)
        except ValueError as err:
            raise forms.ValidationError(str(err)) from err
        return raw


class AgentRunCreateForm(AgentRunFieldsMixin, forms.Form):
    """Validate 'Start a run' submissions. Orchestration lives in ``activity.services``."""
