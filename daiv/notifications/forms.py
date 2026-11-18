from __future__ import annotations

import re

from django import forms
from django.utils.translation import gettext_lazy as _

from notifications.channels.rocketchat import verify_username
from notifications.channels.slack import verify_member_id

_MSG_USERNAME_REQUIRED = _("Username is required.")
_MSG_MEMBER_ID_REQUIRED = _("Member ID is required.")
_MSG_MEMBER_ID_SHAPE = _("Enter a Slack member ID, e.g. U0123ABCD.")

# Slack member IDs start with U (user) or W (workspace app/bot user) and are
# otherwise uppercase alphanumerics. Checking the shape before calling Slack
# stops anyone using the form to probe which member IDs exist.
_MEMBER_ID_RE = re.compile(r"[UW][A-Z0-9]+")


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


class SlackBindingForm(forms.Form):
    # The shared connect partial posts every channel's identifier as ``username``;
    # for Slack that value is the member ID.
    username = forms.CharField(error_messages={"required": _MSG_MEMBER_ID_REQUIRED})

    def clean_username(self) -> str:
        member_id = self.cleaned_data["username"].strip()
        if not member_id:
            raise forms.ValidationError(_MSG_MEMBER_ID_REQUIRED)
        if not _MEMBER_ID_RE.fullmatch(member_id):
            raise forms.ValidationError(_MSG_MEMBER_ID_SHAPE)
        return member_id

    def clean(self):
        cleaned = super().clean()
        member_id = cleaned.get("username")
        if not member_id:
            return cleaned
        verified_id, error = verify_member_id(member_id)
        if error:
            raise forms.ValidationError(error)
        # Save the ID Slack returns rather than the typed value.
        cleaned["username"] = verified_id
        return cleaned
