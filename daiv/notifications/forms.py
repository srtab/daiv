from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from notifications.channels.rocketchat import verify_username

_MSG_USERNAME_REQUIRED = _("Username is required.")


class RocketChatBindingForm(forms.Form):
    username = forms.CharField(error_messages={"required": _MSG_USERNAME_REQUIRED})

    def clean_username(self) -> str:
        username = self.cleaned_data["username"].strip().lstrip("@")
        if not username:
            raise forms.ValidationError(_MSG_USERNAME_REQUIRED)
        return username

    def clean(self):
        cleaned = super().clean()
        username = cleaned.get("username")
        if not username:
            return cleaned
        _rc_user_id, error = verify_username(username)
        if error:
            raise forms.ValidationError(error)
        return cleaned
