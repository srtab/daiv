from __future__ import annotations

from django import forms
from django.core.cache import cache
from django.utils.translation import gettext_lazy as _

from notifications.channels.discord import verify_user_id
from notifications.channels.rocketchat import verify_username

_DISCORD_VERIFY_COOLDOWN_SECONDS = 60

_MSG_USERNAME_REQUIRED = _("Username is required.")
_MSG_DISCORD_USER_ID_REQUIRED = _("User ID is required.")


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


class DiscordBindingForm(forms.Form):
    username = forms.CharField(error_messages={"required": _MSG_DISCORD_USER_ID_REQUIRED})

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_username(self) -> str:
        user_id = self.cleaned_data["username"].strip()
        if not user_id:
            raise forms.ValidationError(_MSG_DISCORD_USER_ID_REQUIRED)
        if not user_id.isascii() or not user_id.isdigit():
            raise forms.ValidationError(_("Enter a valid Discord user ID using digits only."))
        return user_id

    def clean(self):
        cleaned = super().clean()
        user_id = cleaned.get("username")
        if not user_id:
            return cleaned
        cooldown_key = f"notifications:discord-connect:{self.user.pk}"
        if not cache.add(cooldown_key, True, timeout=_DISCORD_VERIFY_COOLDOWN_SECONDS):
            raise forms.ValidationError(_("Please wait a minute before requesting another Discord confirmation."))
        _discord_user_id, error = verify_user_id(user_id)
        if error:
            raise forms.ValidationError(error)
        return cleaned
