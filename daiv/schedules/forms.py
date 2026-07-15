from django import forms

from sessions.forms import AgentRunFieldsMixin, RepoListField

from accounts.models import User
from schedules.models import ScheduledJob, ScheduleTemplate
from schedules.services import clear_irrelevant_frequency_fields as _clear_irrelevant_frequency_fields
from schedules.services import compute_next_run_or_raise


class ScheduledJobCreateForm(AgentRunFieldsMixin, forms.ModelForm):
    """Form for creating a new schedule. Excludes ``is_enabled`` since new schedules are always enabled."""

    class Meta:
        model = ScheduledJob
        # ``sandbox_environment`` is declared on ``AgentRunFieldsMixin`` and must be listed
        # here so ModelForm's save() persists it onto the ScheduledJob instance.
        fields = [
            "name",
            "prompt",
            "repos",
            "frequency",
            "cron_expression",
            "time",
            "run_at",
            "agent_model",
            "agent_thinking_level",
            "notify_on",
            "intent",
            "subscribers",
            "sandbox_environment",
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
        compute_next_run_or_raise(instance)
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
            "agent_model",
            "agent_thinking_level",
            "notify_on",
            "intent",
        ]

    def clean(self):
        return _clear_irrelevant_frequency_fields(super().clean())
