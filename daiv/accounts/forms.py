from django import forms

from accounts.models import APIKey


class APIKeyCreateForm(forms.ModelForm):
    class Meta:
        model = APIKey
        fields = ["name"]
