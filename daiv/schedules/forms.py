from django import forms

from activity.forms import AgentRunFieldsMixin, RepoListField

from accounts.models import User
from schedules.models import Frequency, ScheduledJob, ScheduleTemplate


def _clear_irrelevant_frequency_fields(cleaned_data: dict) -> dict:
    """Drop stale ``cron_expression`` / ``time`` / ``run_at`` so switching frequency in the UI
    doesn't round-trip leftover values that the model's ``_validate_frequency_fields`` would reject.
    """
    frequency = cleaned_data.get("frequency")
    if frequency != Frequency.CUSTOM:
        cleaned_data["cron_expression"] = ""
    if frequency in (Frequency.HOURLY, Frequency.CUSTOM, Frequency.ONCE):
        cleaned_data["time"] = None
    if frequency != Frequency.ONCE:
        cleaned_data["run_at"] = None
    return cleaned_data


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
            "run_at",
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

    def clean(self):
        return _clear_irrelevant_frequency_fields(super().clean())

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


class ScheduleTemplateForm(forms.ModelForm):
    """Admin form for creating/editing schedule templates."""

    repos = RepoListField(required=False)

    class Meta:
        model = ScheduleTemplate
        fields = [
            "name",
            "description",
            "prompt",
            "repos",
            "frequency",
            "cron_expression",
            "time",
            "use_max",
            "notify_on",
        ]

    def clean(self):
        return _clear_irrelevant_frequency_fields(super().clean())
