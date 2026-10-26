from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from plugins.fetch import SHA_RE
from plugins.models import Plugin


class RepoURLForm(forms.Form):
    url = forms.CharField(
        label=_("Repository link"),
        max_length=2048,
        widget=forms.TextInput(attrs={"placeholder": "https://github.com/owner/repo", "autofocus": True}),
    )


class InstallForm(forms.Form):
    url = forms.CharField(max_length=2048, widget=forms.HiddenInput)
    commit = forms.CharField(max_length=40, widget=forms.HiddenInput)
    plugin_paths = forms.MultipleChoiceField(
        label=_("Plugins"), widget=forms.CheckboxSelectMultiple, error_messages={"required": _("Pick a plugin.")}
    )
    scope = forms.ChoiceField(label=_("Install for"), initial=Plugin.Scope.USER)

    def __init__(self, *args, candidates, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plugin_paths"].choices = [(c.plugin_path, c.name) for c in candidates if c.supported]
        choices = [(Plugin.Scope.USER, _("Just me"))]
        if user.is_admin:
            choices.append((Plugin.Scope.GLOBAL, _("Everyone")))
        self.fields["scope"].choices = choices

    def clean_commit(self):
        commit = self.cleaned_data["commit"]
        if not SHA_RE.match(commit):
            raise forms.ValidationError(_("The commit is not valid."))
        return commit
