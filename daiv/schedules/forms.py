from django import forms

from activity.forms import AgentRunFieldsMixin

from accounts.models import User
from schedules.models import Frequency, ScheduledJob


class ScheduledJobCreateForm(AgentRunFieldsMixin, forms.ModelForm):
    """Form for creating a new schedule. Excludes ``is_enabled`` since new schedules are always enabled."""

    class Meta:
        model = ScheduledJob
        fields = [
            "name",
            "prompt",
            "repos",
            "frequency",
            "cron_expression",
            "time",
            "use_max",
            "notify_on",
            "subscribers",
        ]
        widgets = {"subscribers": forms.SelectMultiple(attrs={"class": "hidden"})}

    def __init__(self, *args, owner=None, **kwargs):
        super().__init__(*args, **kwargs)
        if "subscribers" in self.fields:
            qs = User.objects.filter(is_active=True)
            if owner is not None:
                qs = qs.exclude(pk=owner.pk)
            self.fields["subscribers"].queryset = qs
            self.fields["subscribers"].required = False

    def _clean_conditional_fields(self, cleaned_data: dict) -> dict:
        """Clear fields that are irrelevant for the selected frequency."""
        frequency = cleaned_data.get("frequency")
        if frequency != Frequency.CUSTOM:
            cleaned_data["cron_expression"] = ""
        if frequency in (Frequency.HOURLY, Frequency.CUSTOM):
            cleaned_data["time"] = None
        return cleaned_data

    def clean(self):
        cleaned_data = super().clean()
        return self._clean_conditional_fields(cleaned_data)

    def save(self, commit: bool = True) -> ScheduledJob:
        instance = super().save(commit=False)
        if instance.is_enabled:
            instance.compute_next_run()
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class ScheduledJobUpdateForm(ScheduledJobCreateForm):
    """Form for editing an existing schedule. Adds ``is_enabled`` toggle."""

    class Meta(ScheduledJobCreateForm.Meta):
        fields = [*ScheduledJobCreateForm.Meta.fields, "is_enabled"]
