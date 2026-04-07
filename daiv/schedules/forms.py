from django import forms

from schedules.models import Frequency, ScheduledJob


class ScheduledJobCreateForm(forms.ModelForm):
    """Form for creating a new schedule. Excludes ``is_enabled`` since new schedules are always enabled."""

    class Meta:
        model = ScheduledJob
        fields = ["name", "prompt", "repo_id", "ref", "frequency", "cron_expression", "time", "timezone"]

    def _clean_conditional_fields(self, cleaned_data: dict) -> dict:
        """Clear fields that are irrelevant for the selected frequency."""
        frequency = cleaned_data.get("frequency")
        if frequency != Frequency.CUSTOM:
            cleaned_data["cron_expression"] = ""
        if frequency in (Frequency.HOURLY, Frequency.CUSTOM):
            cleaned_data["time"] = None
        return cleaned_data

    def clean(self):
        return self._clean_conditional_fields(super().clean())

    def save(self, commit: bool = True) -> ScheduledJob:
        instance = super().save(commit=False)
        instance.compute_next_run()
        if commit:
            instance.save()
        return instance


class ScheduledJobUpdateForm(ScheduledJobCreateForm):
    """Form for editing an existing schedule. Adds ``is_enabled`` toggle."""

    class Meta(ScheduledJobCreateForm.Meta):
        fields = [*ScheduledJobCreateForm.Meta.fields, "is_enabled"]
