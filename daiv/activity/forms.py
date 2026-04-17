"""Shared form fields for any surface that submits an agent run.

Kept as a plain ``Form`` so both ``forms.Form`` and ``forms.ModelForm`` can
consume it as a mixin.
"""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _


class AgentRunFieldsMixin(forms.Form):
    prompt = forms.CharField(label=_("Prompt"), widget=forms.Textarea(attrs={"rows": 6}), required=True)
    repo_id = forms.CharField(label=_("Repository"), max_length=255, required=True)
    ref = forms.CharField(
        label=_("Branch / ref"),
        max_length=255,
        required=False,
        help_text=_("Leave empty to use the repository default branch."),
    )
    use_max = forms.BooleanField(
        label=_("Use max model"),
        required=False,
        initial=False,
        help_text=_("More capable model with thinking set to high."),
    )
