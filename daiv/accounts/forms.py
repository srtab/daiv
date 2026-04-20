from uuid import uuid4

from django import forms
from django.utils.translation import gettext_lazy as _

from accounts.models import APIKey, Role, User


class APIKeyCreateForm(forms.ModelForm):
    class Meta:
        model = APIKey
        fields = ["name"]


class UserCreateForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["name", "email", "role"]

    def save(self, commit=True):
        user = super().save(commit=False)
        user.username = f"user_{uuid4().hex[:12]}"
        user.set_unusable_password()
        if commit:
            user.save()
        return user


class UserUpdateForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["name", "email", "role", "is_active", "notify_on_jobs"]

    def __init__(self, *args, requesting_user: User, **kwargs):
        super().__init__(*args, **kwargs)
        self.requesting_user = requesting_user

    def clean(self):
        cleaned_data = super().clean()

        is_editing_self = self.instance.pk == self.requesting_user.pk
        new_role = cleaned_data.get("role")
        new_is_active = cleaned_data.get("is_active")

        if is_editing_self and new_is_active is False:
            self.add_error("is_active", _("You cannot deactivate your own account."))

        if is_editing_self and new_role != Role.ADMIN and self.instance.is_last_active_admin():
            self.add_error("role", _("You are the last admin. Promote another user before changing your role."))

        if (
            not is_editing_self
            and self.instance.role == Role.ADMIN
            and new_role != Role.ADMIN
            and self.instance.is_last_active_admin()
        ):
            self.add_error("role", _("This is the last admin. Promote another user before demoting this one."))

        return cleaned_data
