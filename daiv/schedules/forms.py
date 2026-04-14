from django import forms
from django.utils.translation import gettext_lazy as _

from notifications.channels.registry import all_channels
from notifications.choices import NotifyOn

from schedules.models import Frequency, ScheduledJob


def _notify_channel_choices() -> list[tuple[str, str]]:
    return [(cls.channel_type, str(cls.display_name)) for cls in all_channels()]


class ScheduledJobCreateForm(forms.ModelForm):
    """Form for creating a new schedule. Excludes ``is_enabled`` since new schedules are always enabled."""

    notify_channels = forms.MultipleChoiceField(
        choices=(),  # populated in __init__
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label=_("Notification channels"),
    )

    class Meta:
        model = ScheduledJob
        fields = [
            "name",
            "prompt",
            "repo_id",
            "ref",
            "frequency",
            "cron_expression",
            "time",
            "use_max",
            "notify_on",
            "notify_channels",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["notify_channels"].choices = _notify_channel_choices()

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
        cleaned_data = self._clean_conditional_fields(cleaned_data)

        notify_on = cleaned_data.get("notify_on", NotifyOn.NEVER)
        notify_channels = cleaned_data.get("notify_channels") or []

        if notify_on == NotifyOn.NEVER:
            cleaned_data["notify_channels"] = []
        elif not notify_channels:
            self.add_error("notify_channels", _("Select at least one channel when notifications are enabled."))

        return cleaned_data

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
