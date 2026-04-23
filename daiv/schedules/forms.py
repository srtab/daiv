from django import forms

from activity.forms import AgentRunFieldsMixin

from accounts.models import User
from schedules.models import Frequency, ScheduledJob


class ScheduledJobCreateForm(AgentRunFieldsMixin, forms.ModelForm):
    """Form for creating a new schedule. Excludes ``is_enabled`` since new schedules are always enabled."""

    class Meta:
        model = ScheduledJob
        fields = ["name", "prompt", "frequency", "cron_expression", "time", "use_max", "notify_on", "subscribers"]
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

    def _post_clean(self):
        # Mirror the parsed repos onto the model instance so ScheduledJob.clean() sees them.
        repos = self.cleaned_data.get("repos")
        if repos is not None and self.instance is not None:
            self.instance.repos = repos
            first = repos[0]
            self.instance.repo_id = first["repo_id"]
            self.instance.ref = first["ref"]
        # If ``repos`` isn't in cleaned_data (clean_repos_json already raised), the
        # form-level error is already recorded; the instance stays as-is so the model's
        # own clean can proceed on placeholder data without crashing.
        super()._post_clean()

    def _update_errors(self, errors):
        """Remap model-level 'repos' errors to the form's 'repos_json' field."""
        if hasattr(errors, "error_dict") and "repos" in errors.error_dict:
            from django.core.exceptions import ValidationError

            remapped = {}
            for field, errs in errors.error_dict.items():
                remapped["repos_json" if field == "repos" else field] = errs
            errors = ValidationError(remapped)
        super()._update_errors(errors)

    def save(self, commit: bool = True) -> ScheduledJob:
        instance = super().save(commit=False)
        instance.repos = self.cleaned_data["repos"]
        # Back-compat: the scalar repo_id/ref columns still exist (dropped in a later migration).
        # Mirror the first entry so any pre-migration consumers see consistent data.
        first = instance.repos[0]
        instance.repo_id = first["repo_id"]
        instance.ref = first["ref"]
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
