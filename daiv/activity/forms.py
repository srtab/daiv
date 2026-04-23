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

MAX_REPOS_PER_SUBMIT = 20


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

        if not isinstance(parsed, list) or not parsed:
            raise forms.ValidationError(_("Select at least one repository."))
        if len(parsed) > MAX_REPOS_PER_SUBMIT:
            raise forms.ValidationError(_("Select no more than %(n)d repositories.") % {"n": MAX_REPOS_PER_SUBMIT})

        seen: set[tuple[str, str]] = set()
        out: list[dict] = []
        for entry in parsed:
            if not isinstance(entry, dict) or set(entry.keys()) != {"repo_id", "ref"}:
                raise forms.ValidationError(_("Each repository entry must have keys 'repo_id' and 'ref'."))
            repo_id = entry["repo_id"]
            ref = entry["ref"] or ""
            if not isinstance(repo_id, str) or not repo_id.strip():
                raise forms.ValidationError(_("repo_id must be a non-empty string."))
            if not isinstance(ref, str):
                raise forms.ValidationError(_("ref must be a string."))
            key = (repo_id, ref)
            if key in seen:
                raise forms.ValidationError(_("Duplicate repositories are not allowed."))
            seen.add(key)
            out.append({"repo_id": repo_id, "ref": ref})

        self.cleaned_data["repos"] = out
        return raw


class AgentRunCreateForm(AgentRunFieldsMixin, forms.Form):
    """Validate 'Start a run' submissions. Orchestration lives in ``activity.services``."""
