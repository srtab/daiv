from django import forms

from accounts.models import APIKey


class APIKeyCreateForm(forms.ModelForm):
    name = forms.CharField(max_length=128)

    class Meta:
        model = APIKey
        fields = ["name"]
