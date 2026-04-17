"""Shared form fields for any surface that submits an agent run.

Kept as a plain ``Form`` so both ``forms.Form`` and ``forms.ModelForm`` can
consume it as a mixin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import forms
from django.utils.translation import gettext_lazy as _

from asgiref.sync import async_to_sync
from jobs.tasks import run_job_task

from activity.models import TriggerType
from activity.services import acreate_activity

if TYPE_CHECKING:
    from accounts.models import User
    from activity.models import Activity


class AgentRunFieldsMixin(forms.Form):
    prompt = forms.CharField(label=_("Prompt"), widget=forms.Textarea(attrs={"rows": 6}), required=True)
    repo_id = forms.CharField(label=_("Repository"), max_length=255, required=True)
    ref = forms.CharField(
        label=_("Branch / ref"),
        max_length=255,
        required=False,
        help_text=_("Leave empty to use the repository default branch."),
    )
    use_max = forms.BooleanField(
        label=_("Use max model"),
        required=False,
        initial=False,
        help_text=_("More capable model with thinking set to high."),
    )


class AgentRunCreateForm(AgentRunFieldsMixin, forms.Form):
    """Submit a new agent run from the UI (blank form or retry pre-fill)."""

    def submit(self, *, user: User) -> Activity:
        data = self.cleaned_data
        ref = data["ref"] or None

        async def _submit() -> Activity:
            task = await run_job_task.aenqueue(
                repo_id=data["repo_id"], prompt=data["prompt"], ref=ref, use_max=data["use_max"]
            )
            return await acreate_activity(
                trigger_type=TriggerType.UI_JOB,
                task_result_id=task.id,
                repo_id=data["repo_id"],
                ref=data["ref"],
                prompt=data["prompt"],
                use_max=data["use_max"],
                user=user,
            )

        return async_to_sync(_submit)()
